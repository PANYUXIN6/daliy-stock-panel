"""Tiered snapshots, rolling computation state and atomic branch publication."""
from __future__ import annotations

import gzip
import json
import os
import threading
import uuid
import zlib
from bisect import bisect_left
from collections import Counter, OrderedDict
from contextlib import suppress
from datetime import date, timedelta
from functools import cache, lru_cache
from pathlib import Path
from typing import NamedTuple

from app.market_time import cn_now
from app.services.evening_report_archive import (
    ATTEMPT_RETENTION_DAYS,
    AUDIT_RETENTION_DAYS,
    DIAGNOSTIC_RETENTION_DAYS,
    INPUT_RETENTION_DAYS,
    SCHEMA_VERSION,
    STATE_REPORTS,
    pack_report,
    split_report,
    unpack_report,
)
from app.services.evening_report_receipt import (
    valid_acceptance_cutoff,
    valid_receipt_body,
    valid_receipt_day,
)
from app.services.evening_report_receipt import valid_input_id as _valid_input_id

_LOCK = threading.RLock()
_BODY_CACHE_LOCK = threading.Lock()
_VALID_BODIES = OrderedDict()


def last_accepted_reference(pointer: dict) -> tuple[str | None, str | None]:
    if pointer.get('complete'):
        return pointer.get('input_id'), pointer.get('cutoff')
    return pointer.get('last_accepted_input_id'), pointer.get('last_accepted_cutoff')


class _AcceptanceHeader(NamedTuple):
    day: str
    cutoff: str


@lru_cache(maxsize=2048)
def _input_header(path: Path, ctime_ns: int, size: int) -> _AcceptanceHeader | None:
    header = _read_input(path, header_only=True)
    if header is None:
        # Retry transient access failures even if the file has not changed.
        raise ValueError('验收暂不可读')
    return header


def _stored_header(path):
    try:
        stat = path.stat()
        return _input_header(path, stat.st_ctime_ns, stat.st_size)
    except (OSError, ValueError, TypeError):
        return None


def atomic_json(path: Path, value: dict, *, before_commit=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('xb') as stream:
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False, default=str,
                                 separators=(',', ':')).encode('utf-8')
            if path.suffix == '.gz':
                with gzip.GzipFile(fileobj=stream, mode='wb', mtime=0) as compressed:
                    compressed.write(payload)
            else:
                stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if before_commit:
            from app.services.pipeline_jobs import publication_guard

            before_commit()
            with publication_guard():
                os.replace(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict | None:
    try:
        if path.suffix == '.gz':
            with gzip.open(path, 'rt', encoding='utf-8') as stream:
                return json.load(stream)
        return json.loads(path.read_text('utf-8'))
    except FileNotFoundError:
        return None


def _read_history_object(path: Path) -> dict | None:
    """An unreadable historical artifact supplies no proof. Publication reads stay strict."""
    try:
        value = read_json(path)
    except (OSError, EOFError, ValueError, zlib.error):
        return None
    return value if isinstance(value, dict) else None


def _read_input(path: Path, *, header_only=False) -> dict | _AcceptanceHeader | None:
    try:
        with path.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            with gzip.GzipFile(fileobj=stream) as compressed:
                receipt = json.load(compressed)
    except (OSError, EOFError, ValueError, zlib.error):
        return None
    if (not isinstance(receipt, dict) or receipt.get('complete') is not True
            or receipt.get('input_id') != path.name.removesuffix('.json.gz') or not valid_receipt_day(receipt)
            or not valid_acceptance_cutoff(receipt.get('cutoff'))):
        return None
    if header_only:
        return _AcceptanceHeader(receipt['date'], receipt['cutoff'])
    # Cache only the verdict for the file actually opened. Restoration changes its identity.
    identity = (stat.st_dev, stat.st_ino, stat.st_ctime_ns, stat.st_mtime_ns, stat.st_size)
    with _BODY_CACHE_LOCK:
        valid = _VALID_BODIES.get(identity)
    if valid is None:
        valid = valid_receipt_body(receipt)
        with _BODY_CACHE_LOCK:
            _VALID_BODIES[identity] = valid
            if len(_VALID_BODIES) > 2048:
                _VALID_BODIES.popitem(last=False)
    if not valid:
        return None
    receipt.pop('_acceptance_index_day')
    return receipt


def _acceptance_manifest(path: Path) -> dict:
    return {at: ref for at, ref in (_read_history_object(path) or {}).items()
            if valid_acceptance_cutoff(at) and _valid_input_id(ref)}


def report_view(report: dict) -> dict:
    """A response projection of permanent snapshots, excluding audit and state."""
    fields = ('date', 'trade_date', 'next_trade_date', 'cutoff', 'generated_at', 'version',
              'kind', 'mode', 'status', 'rule_version',
              'scope_version', 'selection_policy_version', 'disclaimer', 'summary', 'market', 'records', 'selected', 'limitations',
              'events', 'late_generation', 'recovery_note', 'storage_version', 'retention', 'as_of',
              'wave_rule', 'dip_rule_version')
    result = {key: report[key] for key in fields if key in report}
    result['records'] = [row for row in report.get('records', []) if row.get('selected')]
    result['sectors'] = [{key: value for key, value in sector.items() if key not in ('scores', 'members', 'classification_members')}
                         for sector in report.get('sectors', [])]
    for sector in result['sectors']:
        if 'observation' in sector:
            sector['observation'] = {key: value for key, value in sector['observation'].items()
                                     if key not in ('members', 'lead_source_members', 'missing')}
        sector['ranking_evidence'] = {
            kind: {'status': value.get('status'), 'missing_count': len(value.get('missing', [])),
                   'insufficient_count': len(value.get('insufficient', [])),
                   'missing_components': dict(Counter(item['component'] for item in value.get('missing', []))),
                   'insufficient_regimes': sorted({str(item.get('regime')) for item in value.get('insufficient', [])})}
            for kind, value in sector.get('ranking_evidence', {}).items()}
        change = sector.get('membership_change', {})
        sector['membership_change'] = {key: value for key, value in change.items() if key not in ('added', 'removed')}
        sector['membership_change'].update({key + '_count': len(change.get(key, [])) for key in ('added', 'removed')})
    result['statistics'] = {key: value for key, value in report.get('statistics', {}).items()
                            if key not in ('selection_path_audit', 'identity_continuity_audit', 'legacy_confirmation_audit')}
    provenance = report.get('provenance', {})
    classifications = []
    for item in provenance.get('classifications', []):
        classifications.append({key: value for key, value in item.items() if key not in ('symbols', 'memberships')}
                               | {'symbol_count': item.get('symbol_count', len(item.get('symbols', []))),
                                  'membership_count': item.get('membership_count', len(item.get('memberships', [])))})
    changes = []
    for item in provenance.get('membership_changes', []):
        private = ('added', 'removed', 'first_observed', 'first_observed_symbols')
        changes.append({key: value for key, value in item.items() if key not in private}
                       | {key + '_count': len(item.get(key, [])) for key in ('added', 'removed')}
                       | {'first_observed_count': len(item['first_observed_symbols'])})
    result['provenance'] = {'classifications': classifications, 'membership_changes': changes,
                            'recovery': provenance.get('recovery'),
                            'sector_filter': provenance.get('sector_filter', {}),
                            'suspension_evidence_count': provenance.get('suspension_evidence_count', len(provenance.get('suspensions', [])))}
    return result


def history_view(report: dict) -> dict:
    return {'trade_date': report.get('trade_date') or report.get('date'), 'rule_version': report.get('rule_version'),
            'state': report.get('state', {}),
            **({'scope_version': report['scope_version']} if report.get('scope_version') else {}),
            'records': [{key: row.get(key) for key in ('symbol', 'purpose')} for row in report.get('records', [])],
            'statistics': {key: report.get('statistics', {}).get(key) for key in
                           ('publication_frequency', 'selected_count', 'zero_selection_only_risk_unknown')},
            'input_reference': {'input_id': report.get('input_reference', {}).get('input_id')}}


def _metadata(report: dict) -> dict:
    return {key: report.get(key) for key in
            ('version', 'kind', 'generated_at', 'rule_version', 'scope_version', 'selection_policy_version', 'storage_version')} | {
                'input_id': report.get('input_reference', {}).get('input_id')}


class EveningReportStore:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir).resolve() / 'evening_report'

    def _path(self, day: str) -> Path:
        return self.root / 'reports' / f'{date.fromisoformat(day)}.json'

    def original_metadata(self, day: str, version: str | None = None) -> dict | None:
        entries = (read_json(self._path(day)) or {}).get('versions', [])
        if entries and (version is None or entries[0]['version'] == version):
            return entries[0]
        return None

    def _base(self, entry: dict) -> Path:
        return self.root / 'archive' / entry['version']

    def audit_rows(self, day: str, version: str) -> list[dict] | None:
        entry = self.original_metadata(day, version)
        if not entry:
            return None
        audit = read_json(self._base(entry).with_suffix('.audit.json.gz'))
        return audit['records'] if audit else []

    def candidate_boundary(self, day: str, version: str) -> dict | None:
        entry = self.original_metadata(day, version)
        return read_json(self._base(entry).with_suffix('.candidates.json.gz')) if entry else None

    def waiting_wave_bars(self, day: str) -> dict:
        return (read_json(self._path(day)) or {}).get('waiting_wave_bars', {}) if day else {}

    def state(self, day: str, version: str | None = None) -> dict | None:
        entry = self.original_metadata(day, version)
        return read_json(self._base(entry).with_suffix('.state.json.gz')) if entry else None

    def _load(self, entry):
        packed = read_json(self._base(entry).with_suffix('.json.gz'))
        return unpack_report(packed) if packed is not None else None

    def get(self, day: str, version: str | None = None, *, view: bool = False) -> dict | None:
        entry = self.original_metadata(day, version)
        report = self._load(entry) if entry else None
        if not report or not view:
            return report
        from app.services.evening_report_wave_context import project_context_refs

        public = report_view(report)
        public['records'] = [dict(row) for row in public['records']]
        return project_context_refs(public, self, report)

    def referenced_report(self, day: str, version: str | None = None):
        """A damaged optional reference supplies no context to its consumer."""
        try:
            return self.get(day, version)
        except (OSError, EOFError, ValueError, zlib.error):
            return None

    def has_original(self, day: str) -> bool:
        return self.original_metadata(day) is not None

    def published_wave_lookups(self, before: str):
        """Find immutable starts, structural events and first window closures in the original publications."""
        from app.services.evening_report_wave import (
            STRUCTURE_EVENT_TYPES,
            start_key,
            structure_event_key,
        )

        days = sorted(path.stem for path in (self.root / 'reports').glob('*.json') if path.stem < before)
        if not days:
            return None, None, None

        @cache
        def publication(day):
            pointer = read_json(self._path(day)) or {}
            entries = pointer.get('versions') or []
            return (entries[0] if entries else None,
                    {tuple(key) for key in pointer.get('wave_start_keys', ())}, pointer.get('wave_window_closes', {}))

        @cache
        def evidence(day):
            original, _, _ = publication(day)
            report = self._load(original) or {}
            starts = {start_key(event): event for event in report.get('wave_starts', ())}
            structure_events = {}
            for event in report.get('wave_events', ()):
                if event['type'] in STRUCTURE_EVENT_TYPES and event.get('round_id'):
                    structure_events.setdefault(structure_event_key(event), event)
            return starts, structure_events

        def start_lookup(key, since):
            for day in days[bisect_left(days, since):]:
                original, keys, _ = publication(day)
                if original and key in keys:
                    event = evidence(day)[0].get(key)
                    if event is not None:
                        return event
            return None

        def close_lookup(round_id, since):
            for day in days[bisect_left(days, since):]:
                original, _, closes = publication(day)
                if original:
                    close = closes.get(round_id)
                    if close is not None:
                        return close
            return None

        def structure_event_lookup(event):
            key = structure_event_key(event)
            for day in days[bisect_left(days, event['date']):]:
                if publication(day)[0] and (saved := evidence(day)[1].get(key)):
                    return saved
            return None

        return start_lookup, close_lookup, structure_event_lookup

    def dates(self) -> list[dict]:
        result = []
        for path in sorted((self.root / 'reports').glob('*.json'), reverse=True):
            entries = (read_json(path) or {}).get('versions', [])
            if entries:
                result.append({'date': path.stem, 'status': 'published', 'versions': entries})
        return result

    def history(self, before: str, rule_version: str, *, allow_partial: bool = False) -> list[dict]:
        # All long-lived continuity is forwarded in state. Seven actual reports
        # cover every finite retrospective decision; calendar gaps stay explicit.
        selected = []
        for entry in self.dates():
            if entry['date'] >= before:
                continue
            original = entry['versions'][0]
            if original.get('rule_version') == rule_version:
                selected.append((entry['date'], original))
                if len(selected) == STATE_REPORTS:
                    break
        history = []
        for day, entry in reversed(selected):
            state = _read_history_object(self._base(entry).with_suffix('.state.json.gz'))
            if state is None and allow_partial and day != selected[0][0]:
                continue  # Date-keyed lookbacks expose a missing observation as unknown.
            if state is None:
                from app.services.daily_coverage import DailyCoverageError
                raise DailyCoverageError(f'{day} 的完整运行状态已过保留期，不能精确重放此前全市场筛选')
            history.append(state)
        return history

    def _write(self, path: Path, report: dict) -> dict:
        permanent, audit, boundary = split_report(report)
        packed = pack_report(permanent)
        atomic_json(path.with_suffix('.json.gz'), packed)
        atomic_json(path.with_suffix('.audit.json.gz'), audit)
        atomic_json(path.with_suffix('.candidates.json.gz'), boundary)
        atomic_json(path.with_suffix('.state.json.gz'), history_view(report))
        return unpack_report(packed)

    def save(self, day: str, payload: dict, input_reference: dict, *, before_publish=None) -> dict:
        with _LOCK:
            original = self.get(day)
            if original:
                return original
            report = {**payload, 'date': day, 'mode': 'live', 'status': 'published',
                      'storage_version': SCHEMA_VERSION, 'version': uuid.uuid4().hex, 'kind': 'original',
                      'generated_at': cn_now().isoformat(), 'input_reference': input_reference}
            if report.get('wave_rule'):
                from app.services.evening_report_wave_context import add_context_refs

                add_context_refs(report, self)
            path = self.root / 'archive' / report['version']
            report = self._write(path, report)
            # Readers see the complete tiered publication only after this pointer.
            from app.services.evening_report_wave import start_key

            pointer = {'versions': [_metadata(report)],
                       'wave_start_keys': [start_key(event) for event in report.get('wave_starts', [])]}
            if 'wave_window_closes' in payload:
                pointer['wave_window_closes'] = payload['wave_window_closes']
            waiting = {}
            for symbol, stock in payload.get('state', {}).get('wave', {}).get('stocks', {}).items():
                for session, cutoff in stock['state'].get('waiting_for_bars', {}).items():
                    waiting.setdefault(session, {})[symbol] = cutoff
            for window in payload.get('state', {}).get('study_windows', {}).values():
                for session, symbols in window.get('missing', {}).items():
                    for symbol in symbols:
                        waiting.setdefault(session, {}).setdefault(symbol, payload['cutoff'])
            if waiting:
                pointer['waiting_wave_bars'] = waiting
            atomic_json(self._path(day), pointer, before_commit=before_publish)
            return report

    def input_evidence(self, input_id: str) -> dict | None:
        if not _valid_input_id(input_id):
            raise ValueError('输入证据标识无效')
        path = self.root / 'inputs' / f'{input_id}.json.gz'
        if receipt := _read_input(path):
            return receipt
        staged = path.parent / 'pending' / path.name
        receipt = _read_input(staged)
        if not receipt:
            return None
        day, at = receipt['date'], receipt['cutoff']
        if (_acceptance_manifest(self.root / 'acceptances' / f'{day}.json').get(at) == input_id
                or (at, input_id) in self.acceptance_references().get(day, ())):
            return receipt
        return None

    def accepted_input(self, input_id, cutoff, *, day=None):
        """Read the original identified by a successful acceptance reference."""
        receipt = self.input_evidence(input_id) if input_id and cutoff else None
        if receipt and receipt['cutoff'] == cutoff and (day is None or receipt['date'] == day):
            return receipt
        return None

    def latest_readable_input(self, candidates):
        """An unreadable success still orders batches, but supplies no stock facts."""
        for at, ref in sorted(candidates, reverse=True):
            if receipt := self.accepted_input(ref, at):
                return receipt
        return None

    def readiness_status(self, day: str) -> dict | None:
        pointer = _read_history_object(self.root / 'ready' / f'{date.fromisoformat(day)}.json')
        if pointer and 'pending_acceptance' in pointer:
            pending = pointer.pop('pending_acceptance')
            if isinstance(pending, dict) and pending.get('complete') is True and pending.get('date', day) == day:
                input_id, at = last_accepted_reference(pending)
                if (_valid_input_id(input_id) and valid_acceptance_cutoff(at)
                        and _acceptance_manifest(self.root / 'acceptances' / f'{day}.json').get(at) == input_id):
                    pointer = pending
            pointer = pointer or None
        if pointer:
            if not isinstance(pointer.get('complete'), bool) or pointer.get('date', day) != day:
                return None
            input_id, cutoff = last_accepted_reference(pointer)
            if (pointer['complete'] or input_id is not None) and not _valid_input_id(input_id):
                return None
            if input_id is not None and not valid_acceptance_cutoff(cutoff):
                return None
        return pointer

    def readiness(self, day: str) -> dict | None:
        pointer = self.readiness_status(day)
        if pointer and pointer['complete']:
            return self.accepted_input(pointer['input_id'], pointer['cutoff'], day=day)
        return pointer

    def _committed_entries(self, day):
        entries = _acceptance_manifest(self.root / 'acceptances' / f'{date.fromisoformat(day)}.json')
        directory = self.root / 'inputs'
        for at, ref in list(entries.items()):
            header = (_stored_header(directory / f'{ref}.json.gz')
                      or _stored_header(directory / 'pending' / f'{ref}.json.gz'))
            if header and (header.day != day or header.cutoff != at):
                del entries[at]
        return entries

    def acceptance_references(self) -> dict[str, list[tuple[str, str]]]:
        """Combine committed cutoff entries with receipts promoted after the index switch."""
        dates = {}
        indexed = set()
        for path in (self.root / 'acceptances').glob('*.json'):
            try:
                day = date.fromisoformat(path.stem).isoformat()
            except ValueError:
                continue
            if entries := self._committed_entries(day):
                dates[day] = entries
                indexed.update(entries.values())
        directory = self.root / 'inputs'
        # A final (or revoked) pointer may be the last record of a missing input
        # after index loss. Keep its cutoff as a gap, including through maintenance.
        for path in (self.root / 'ready').glob('*.json'):
            try:
                pointer = self.readiness_status(path.stem)
            except ValueError:
                continue
            ref, at = last_accepted_reference(pointer or {})
            if not ref or dates.get(path.stem, {}).get(at) == ref:
                continue
            name = f'{ref}.json.gz'
            header = _stored_header(directory / name) or _stored_header(directory / 'pending' / name)
            if header and (header.day != path.stem or header.cutoff != at):
                continue
            if dates.setdefault(path.stem, {}).setdefault(at, ref) == ref:
                indexed.add(ref)
        for path in directory.glob('*.json.gz'):
            ref = path.name.removesuffix('.json.gz')
            if ref not in indexed and _valid_input_id(ref) and (header := _stored_header(path)):
                dates.setdefault(header.day, {}).setdefault(header.cutoff, ref)
        for path in (directory / 'pending').glob('*.json.gz'):
            ref = path.name.removesuffix('.json.gz')
            if ref in indexed or not _valid_input_id(ref):
                continue
            header = _stored_header(path)
            if header and (self.original_metadata(header.day) or {}).get('input_id') == ref:
                dates.setdefault(header.day, {}).setdefault(header.cutoff, ref)
        return {day: sorted(entries.items()) for day, entries in dates.items()}

    def historical_reference(self, day, references=None):
        """Select the latest recorded acceptance, including a reference to an unavailable payload."""
        input_id, cutoff = last_accepted_reference(self.readiness_status(day) or {})
        entries = references.get(day, ()) if references is not None else sorted(self._committed_entries(day).items())
        if entries:
            last_at, last_id = entries[-1]
            if (not input_id and references is not None and (self.original_metadata(day) or {}).get('input_id') == last_id
                    and self._committed_entries(day).get(last_at) != last_id):
                return None, None
            if not input_id or input_id == last_id or (cutoff and cutoff <= last_at):
                return last_id, last_at
        return input_id, cutoff

    def last_accepted_input_id(self, day: str, *, after: str | None = None) -> str | None:
        """Skip a receipt already covered by its pointer's cutoff."""
        pointer = self.readiness_status(day) or {}
        input_id, cutoff = last_accepted_reference(pointer)
        if after and cutoff and cutoff <= after:
            return None
        return input_id

    def save_readiness(self, day: str, evidence: dict, *, before_commit=None) -> None:
        ready = self.root / 'ready' / f'{date.fromisoformat(day)}.json'
        with _LOCK:
            previous = self.readiness_status(day)
            if not evidence.get('complete'):
                pointer = dict(evidence)
                last_accepted, cutoff = last_accepted_reference(previous or {})
                if last_accepted:
                    pointer['last_accepted_input_id'] = last_accepted
                    if cutoff:
                        pointer['last_accepted_cutoff'] = cutoff
                atomic_json(ready, pointer)
                return

            cutoff = evidence.get('cutoff')
            if evidence.get('date') != day or not valid_acceptance_cutoff(cutoff):
                raise ValueError('完整验收必须包含对应交易日和有效截止时间')
            input_id = evidence.setdefault('input_id', uuid.uuid4().hex)
            if not _valid_input_id(input_id):
                raise ValueError('输入证据标识无效')
            path = self.root / 'inputs' / f'{input_id}.json.gz'
            staged = path.parent / 'pending' / path.name
            existing_path = next((file for file in (path, staged) if file.exists()), None)
            if existing_path and _read_input(existing_path) != json.loads(json.dumps(evidence, default=str, allow_nan=False)):
                raise ValueError('既有输入证据不可覆写，请创建新的输入版本')
            from app.tickflow.daily_revisions import track_receipt
            track_receipt(self.root, day)
            pointer = {'date': day, 'complete': True, 'input_id': input_id,
                       'cutoff': cutoff, 'scope_version': evidence.get('scope_version')}
            index = self.root / 'acceptances' / f'{day}.json'
            references = self._committed_entries(day)
            prior_id, prior_at = last_accepted_reference(previous or {})
            if existing_path is None:
                if input_id in references.values() or input_id == prior_id:
                    raise ValueError('原验收文件缺失，请为新验收创建新的输入版本')
                atomic_json(staged, {**evidence, '_acceptance_index_day': day})
            publish_history = references.get(cutoff) != input_id
            if not publish_history:
                # Without a new history entry only the live authorization needs to switch.
                atomic_json(ready, pointer, before_commit=before_commit)
            else:
                if prior_at and prior_id:
                    references.setdefault(prior_at, prior_id)
                # Readers of the readiness pointer use its previous value until the index commits.
                atomic_json(ready, {**(previous or {}), 'pending_acceptance': pointer})
                references[cutoff] = input_id
                atomic_json(index, references, before_commit=before_commit)
            if not existing_path or existing_path == staged:
                # Already committed. Readers can resolve the staged copy through the index.
                with suppress(OSError):
                    os.replace(staged, path)
            # A finalized pointer can still witness its cutoff if the index and original are later lost.
            if publish_history:
                with suppress(OSError):
                    atomic_json(ready, pointer)

    def save_diagnostic(self, report: dict, *, reason: str) -> None:
        """Only explicit per-run diagnostics retain full intermediate results."""
        now = cn_now()
        atomic_json(self.root / 'diagnostics' / f'{now.date()}-{uuid.uuid4().hex}.json.gz', {
            'created_at': now.isoformat(), 'expires_at': (now + timedelta(days=DIAGNOSTIC_RETENTION_DAYS)).isoformat(),
            'trigger': reason, 'report': report})

    def prune(self, today: date, cancel_check=None) -> dict:
        """Called in the collector write slot, never by a reader or web request.

        Permanent recommendation/candidate snapshots and personal data are not
        deletion targets. The newest accepted input and latest seven original
        states remain usable even after a long outage.
        """
        check = cancel_check or (lambda: None)
        if not self.root.exists():
            return {'removed_files': 0, 'removed_bytes': 0}
        if self.root.is_symlink() or self.root.resolve() != self.root.absolute():
            raise ValueError('晚报维护目录不能是符号链接')
        entries = self.dates()
        states = {day['versions'][0]['version'] for day in entries[:STATE_REPORTS]}
        targets = []
        audit_cutoff = (today - timedelta(days=AUDIT_RETENTION_DAYS)).isoformat()
        for day in entries:
            entry = day['versions'][0]
            base = self._base(entry)
            if day['date'] < audit_cutoff:
                targets.extend(base.with_suffix(suffix) for suffix in ('.audit.json.gz', '.candidates.json.gz'))
            if entry['version'] not in states:
                targets.append(base.with_suffix('.state.json.gz'))
        expiry_timestamp = cn_now().replace(year=today.year, month=today.month, day=today.day,
                                           hour=0, minute=0, second=0, microsecond=0).timestamp() - INPUT_RETENTION_DAYS * 86400
        input_cutoff_day = (today - timedelta(days=INPUT_RETENTION_DAYS)).isoformat()
        references = self.acceptance_references()
        accepted = sorted(((at, ref, day) for day, entries in references.items() for at, ref in entries), reverse=True)
        directory = self.root / 'inputs'
        # Protect the latest available complete acceptance by its own cutoff, even if
        # restoring an older original has made that file appear newer on disk.
        retained = set()
        for at, ref, day in accepted:
            check()
            name = f'{ref}.json.gz'
            receipt = _read_input(directory / name) or _read_input(directory / 'pending' / name)
            if receipt and receipt['date'] == day and receipt['cutoff'] == at:
                retained.add(ref)
                break
        if accepted:
            retained.add(accepted[0][1])  # Preserve the cutoff of a missing or unreadable last input.
        accepted_ids = {ref for _, ref, _ in accepted}
        expired_inputs = []
        for folder in (directory, directory / 'pending'):
            for path in folder.glob('*.json.gz'):
                ref = path.name.removesuffix('.json.gz')
                if not _valid_input_id(ref) or ref in retained:
                    continue
                if path.stat().st_mtime < expiry_timestamp:
                    expired_inputs.append((ref, path))
                elif ref in accepted_ids:
                    retained.add(ref)
        targets.extend(path for ref, path in expired_inputs if ref not in retained)
        for path in (self.root / 'acceptances').glob('*.json'):
            if path.stem >= input_cutoff_day:
                continue
            manifest = _read_history_object(path)
            if manifest is not None and all(_valid_input_id(ref) and ref not in retained for ref in manifest.values()):
                targets.append(path)
        writes = self.root / 'price_writes'
        targets.extend(path for pattern in ('????-??-??_*.json*', 'factor_*.json*')
                       for path in writes.glob(pattern) if path.stat().st_mtime < expiry_timestamp)
        # A process may die after staging files but before publishing the index.
        # Only our UUID-named artifacts older than the temporary-input TTL qualify.
        versions = {day['versions'][0]['version'] for day in entries}
        for path in (self.root / 'archive').glob('*.gz'):
            identifier = path.name.split('.')[0]
            if (len(identifier) == 32 and all(c in '0123456789abcdef' for c in identifier)
                    and identifier not in versions and path.stat().st_mtime < expiry_timestamp):
                targets.append(path)
        for path in (self.root / 'diagnostics').glob('*.json.gz'):
            if path.name[:10] < (today - timedelta(days=DIAGNOSTIC_RETENTION_DAYS)).isoformat():
                targets.append(path)
        for directory, days in (('attempts', ATTEMPT_RETENTION_DAYS), ('ready', INPUT_RETENTION_DAYS)):
            for path in (self.root / directory).glob('*.json'):
                if path.stem < (today - timedelta(days=days)).isoformat():
                    if directory == 'ready':
                        try:
                            pointer = self.readiness_status(path.stem)
                        except ValueError:
                            continue
                        if pointer is None:
                            continue
                        input_id, _ = last_accepted_reference(pointer)
                        if input_id in retained:
                            continue
                    targets.append(path)
        count = size = 0
        for path in targets:
            check()
            if not path.exists():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
                raise ValueError('晚报维护目标超出模块目录')
            size += path.stat().st_size
            path.unlink()
            count += 1
        return {'removed_files': count, 'removed_bytes': size}
