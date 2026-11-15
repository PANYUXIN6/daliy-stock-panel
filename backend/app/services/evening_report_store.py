"""Tiered snapshots, rolling computation state and atomic branch publication."""
from __future__ import annotations

import gzip
import json
import os
import shutil
import threading
import uuid
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from app.market_time import cn_now
from app.services.evening_report_archive import (
    ATTEMPT_RETENTION_DAYS,
    AUDIT_RETENTION_DAYS,
    DIAGNOSTIC_RETENTION_DAYS,
    INPUT_RETENTION_DAYS,
    SCHEMA_VERSION,
    STATE_REPORTS,
    compactable,
    pack_report,
    split_report,
    unpack_report,
)

_LOCK = threading.RLock()


def atomic_json(path: Path, value: dict) -> None:
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


def report_view(report: dict) -> dict:
    """A response projection of permanent snapshots, excluding audit and state."""
    fields = ('date', 'trade_date', 'next_trade_date', 'cutoff', 'generated_at', 'version',
              'parent_version', 'branch_id', 'kind', 'mode', 'status', 'rule_version',
              'scope_version', 'selection_policy_version', 'disclaimer', 'summary', 'market', 'records', 'selected', 'limitations',
              'events', 'late_generation', 'recovery_note', 'storage_version', 'retention', 'as_of')
    result = {key: report[key] for key in fields if key in report}
    if report.get('selection_policy_version') == 'theme-observation-v1':
        # Comparable candidates stay in the archive, not every table response.
        result['records'] = [row for row in report.get('records', []) if row.get('selected')]
    result['sectors'] = [{key: value for key, value in sector.items() if key not in ('scores', 'members', 'classification_members')}
                         for sector in report.get('sectors', [])]
    for sector in result['sectors']:
        if 'observation' in sector:
            sector['observation'] = {key: value for key, value in sector['observation'].items()
                                     if key not in ('members', 'missing')}
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
        changes.append({key: value for key, value in item.items() if key not in ('added', 'removed', 'first_observed')}
                       | {key + '_count': len(item.get(key, [])) for key in ('added', 'removed', 'first_observed')})
    result['provenance'] = {'classifications': classifications, 'membership_changes': changes,
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
            ('version', 'kind', 'generated_at', 'parent_version', 'branch_id', 'rule_version', 'scope_version', 'selection_policy_version', 'storage_version')} | {
                'input_id': report.get('input_reference', {}).get('input_id')}


class EveningReportStore:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir).resolve() / 'evening_report'

    def _path(self, day: str) -> Path:
        return self.root / 'reports' / f'{date.fromisoformat(day)}.json'

    def branch_manifests(self) -> list[dict]:
        return [read_json(path) for path in sorted((self.root / 'branches').glob('*.json'))]

    def version_metadata(self, day: str) -> list[dict]:
        entries = (read_json(self._path(day)) or {}).get('versions', [])
        for manifest in self.branch_manifests():
            if day in manifest['dates']:
                entries.append(manifest['dates'][day])
        return entries

    def _load(self, day: str, entry: dict, *, view: bool = False) -> dict | None:
        if entry.get('storage_version') == SCHEMA_VERSION:
            path = self._base(day, entry).with_suffix('.json.gz')
            result = read_json(path)
            if result is None:
                return None
            result = unpack_report(result)
            return report_view(result) if view else result
        suffix = '.view.json' if view else '.json'
        if entry.get('branch_id'):
            path = self.root / 'revisions' / entry['branch_id'] / f'{day}{suffix}'
        else:
            path = self.root / 'archive' / f"{entry['version']}{suffix}"
        return read_json(path)

    def _base(self, day: str, entry: dict) -> Path:
        if entry.get('branch_id'):
            return self.root / 'revisions' / entry['branch_id'] / day
        return self.root / 'archive' / entry['version']

    def audit_rows(self, day: str, version: str) -> list[dict] | None:
        entry = next((item for item in self.version_metadata(day) if item['version'] == version), None)
        if not entry or entry.get('storage_version') != SCHEMA_VERSION:
            return None
        audit = read_json(self._base(day, entry).with_suffix('.audit.json.gz'))
        return audit['records'] if audit else []

    def candidate_boundary(self, day: str, version: str) -> dict | None:
        entry = next((item for item in self.version_metadata(day) if item['version'] == version), None)
        if not entry or entry.get('storage_version') != SCHEMA_VERSION:
            return None
        return read_json(self._base(day, entry).with_suffix('.candidates.json.gz'))

    def state(self, day: str, version: str | None = None) -> dict | None:
        entry = (next((item for item in self.version_metadata(day) if item['version'] == version), None)
                 if version else self.original_metadata(day))
        if not entry:
            return None
        suffix = '.state.json.gz' if entry.get('storage_version') == SCHEMA_VERSION else '.state.json'
        return read_json(self._base(day, entry).with_suffix(suffix))

    def versions(self, day: str) -> list[dict]:
        return [self._load(day, entry) for entry in self.version_metadata(day)]

    def get(self, day: str, version: str | None = None, *, view: bool = False) -> dict | None:
        entries = self.version_metadata(day)
        entry = next((item for item in entries if item['version'] == version), None) if version else next(
            (item for item in entries if item['kind'] == 'original'), None)
        return self._load(day, entry, view=view) if entry else None

    def original_metadata(self, day: str) -> dict | None:
        return next((item for item in (read_json(self._path(day)) or {}).get('versions', [])
                     if item['kind'] == 'original'), None)

    def has_original(self, day: str) -> bool:
        return self.original_metadata(day) is not None

    def dates(self) -> list[dict]:
        result = {}
        for path in sorted((self.root / 'reports').glob('*.json'), reverse=True):
            entries = (read_json(path) or {}).get('versions', [])
            if entries:
                result[path.stem] = {'date': path.stem, 'status': 'published', 'versions': entries}
        for manifest in self.branch_manifests():
            for day, metadata in manifest['dates'].items():
                result.setdefault(day, {'date': day, 'status': 'published', 'versions': []})['versions'].append(metadata)
        return sorted(result.values(), key=lambda entry: entry['date'], reverse=True)

    def history(self, before: str, rule_version: str) -> list[dict]:
        # All long-lived continuity is forwarded in state. Seven actual reports
        # cover every finite retrospective decision; calendar gaps stay explicit.
        selected = []
        for entry in self.dates():
            if entry['date'] >= before:
                continue
            original = next((v for v in entry['versions'] if v['kind'] == 'original'
                             and v.get('rule_version') == rule_version), None)
            if original:
                selected.append((entry['date'], original))
                if len(selected) == STATE_REPORTS:
                    break
        history = []
        for day, entry in reversed(selected):
            state = self.state(day, entry['version'])
            if state is None:
                from app.services.daily_coverage import DailyCoverageError
                raise DailyCoverageError(f'{day} 的完整运行状态已过保留期，不能精确重放此前全市场筛选')
            history.append(state)
        return history

    def _write(self, path: Path, report: dict) -> dict:
        if not compactable(report):
            atomic_json(path.with_suffix('.json'), report)
            atomic_json(path.with_suffix('.view.json'), report_view(report))
            atomic_json(path.with_suffix('.state.json'), history_view(report))
            return report
        permanent, audit, boundary = split_report(report)
        packed = pack_report(permanent)
        atomic_json(path.with_suffix('.json.gz'), packed)
        atomic_json(path.with_suffix('.audit.json.gz'), audit)
        atomic_json(path.with_suffix('.candidates.json.gz'), boundary)
        atomic_json(path.with_suffix('.state.json.gz'), history_view(report))
        return unpack_report(packed)

    def save(self, day: str, payload: dict, input_reference: dict) -> dict:
        with _LOCK:
            original = self.get(day)
            if original:
                return original
            report = {**payload, 'date': day, 'mode': 'live', 'status': 'published',
                      'version': uuid.uuid4().hex, 'kind': 'original', 'parent_version': None,
                      'generated_at': cn_now().isoformat(), 'input_reference': input_reference}
            path = self.root / 'archive' / report['version']
            report = self._write(path, report)
            # Readers see the complete tiered publication only after this pointer.
            atomic_json(self._path(day), {'versions': [_metadata(report)]})
            return report

    def input_evidence(self, input_id: str) -> dict | None:
        if len(input_id) != 32 or any(c not in '0123456789abcdef' for c in input_id):
            raise ValueError('输入证据标识无效')
        return (read_json(self.root / 'inputs' / f'{input_id}.json.gz')
                or read_json(self.root / 'inputs' / f'{input_id}.json'))

    def readiness_status(self, day: str) -> dict | None:
        return read_json(self.root / 'ready' / f'{date.fromisoformat(day)}.json')

    def readiness(self, day: str) -> dict | None:
        pointer = self.readiness_status(day)
        if pointer and pointer.get('input_id'):
            return self.input_evidence(pointer['input_id'])
        return pointer

    def save_readiness(self, day: str, evidence: dict) -> None:
        if evidence.get('complete'):
            evidence.setdefault('input_id', uuid.uuid4().hex)
            path = self.root / 'inputs' / f"{evidence['input_id']}.json.gz"
            existing = self.input_evidence(evidence['input_id'])
            if existing is not None and existing != json.loads(json.dumps(evidence, default=str, allow_nan=False)):
                raise ValueError('既有输入证据不可覆写，请创建新的输入版本')
            if existing is None:
                atomic_json(path, evidence)
            pointer = {'date': day, 'complete': True, 'input_id': evidence['input_id'],
                       'cutoff': evidence.get('cutoff'), 'scope_version': evidence.get('scope_version')}
        else:
            pointer = evidence
        atomic_json(self.root / 'ready' / f'{date.fromisoformat(day)}.json', pointer)

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
        originals = [entry for day in entries for entry in day['versions'] if entry['kind'] == 'original']
        states = {entry['version'] for entry in originals[:STATE_REPORTS]}
        targets = []
        audit_cutoff = (today - timedelta(days=AUDIT_RETENTION_DAYS)).isoformat()
        for day in entries:
            for entry in day['versions']:
                if entry.get('storage_version') != SCHEMA_VERSION:
                    continue  # Historical formats are readable, never silently rewritten.
                base = self._base(day['date'], entry)
                if day['date'] < audit_cutoff:
                    targets.extend(base.with_suffix(suffix) for suffix in ('.audit.json.gz', '.candidates.json.gz'))
                if entry['version'] not in states:
                    targets.append(base.with_suffix('.state.json.gz'))
        expiry_timestamp = cn_now().replace(year=today.year, month=today.month, day=today.day,
                                           hour=0, minute=0, second=0, microsecond=0).timestamp() - INPUT_RETENTION_DAYS * 86400
        # The legacy writer used plain JSON. Neither those inputs nor inputs
        # shared with full-format originals/revisions belong to the new TTL.
        protected_inputs = {entry.get('input_id') for day in entries for entry in day['versions']
                            if entry.get('storage_version') != SCHEMA_VERSION}
        protected_inputs.update(path.stem for path in (self.root / 'inputs').glob('*.json'))
        inputs = sorted((self.root / 'inputs').glob('*.json.gz'), key=lambda path: path.stat().st_mtime_ns, reverse=True)
        expired_inputs = set()
        for path in inputs[1:]:
            input_id = path.name.removesuffix('.json.gz')
            if input_id not in protected_inputs and path.stat().st_mtime < expiry_timestamp:
                targets.append(path)
                expired_inputs.add(input_id)
        # A process may die after staging files but before publishing the index.
        # Only our UUID-named artifacts older than the temporary-input TTL qualify.
        versions = {entry['version'] for day in entries for entry in day['versions']}
        branches = {entry.get('branch_id') for day in entries for entry in day['versions']}
        for path in (self.root / 'archive').glob('*.gz'):
            identifier = path.name.split('.')[0]
            if (len(identifier) == 32 and all(c in '0123456789abcdef' for c in identifier)
                    and identifier not in versions and path.stat().st_mtime < expiry_timestamp):
                targets.append(path)
        for directory in (self.root / 'revisions').iterdir() if (self.root / 'revisions').is_dir() else []:
            identifier = directory.name
            if (directory.is_dir() and len(identifier) == 32 and all(c in '0123456789abcdef' for c in identifier)
                    and identifier not in branches and directory.stat().st_mtime < expiry_timestamp):
                targets.extend(path for path in directory.glob('*.json.gz') if path.is_file())
        for path in (self.root / 'diagnostics').glob('*.json.gz'):
            if path.name[:10] < (today - timedelta(days=DIAGNOSTIC_RETENTION_DAYS)).isoformat():
                targets.append(path)
        for directory, days in (('attempts', ATTEMPT_RETENTION_DAYS), ('ready', INPUT_RETENTION_DAYS)):
            for path in (self.root / directory).glob('*.json'):
                if path.stem < (today - timedelta(days=days)).isoformat():
                    if directory == 'ready':
                        pointer = read_json(path) or {}
                        if pointer.get('complete') and pointer.get('input_id') not in expired_inputs:
                            continue  # Preserve the entry point to retained/unknown input formats.
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


class RevisionBranch:
    """Unpublished files become readable only through one atomic manifest commit."""
    def __init__(self, store: EveningReportStore):
        self.store = store
        self.id = uuid.uuid4().hex
        self.root = store.root / 'revisions' / self.id
        self.dates: dict[str, dict] = {}
        self.references: dict[str, str] = {}
        self.rule_version = None
        self.scope_version = None
        self.selection_policy_version = None
        self.gaps: list[str] = []
        self.committed = False

    def append(self, day: str, payload: dict, reference: dict) -> dict:
        original = self.store.original_metadata(day)
        report = {**payload, 'date': day, 'mode': 'live', 'status': 'published',
                  'version': uuid.uuid4().hex, 'kind': 'revision', 'branch_id': self.id,
                  'parent_version': original['version'] if original else None,
                  'generated_at': cn_now().isoformat(), 'input_reference': reference}
        report = self.store._write(self.root / str(date.fromisoformat(day)), report)
        self.references[day] = reference['input_id']
        self.rule_version = payload.get('rule_version')
        self.scope_version = payload.get('scope_version')
        self.selection_policy_version = payload.get('selection_policy_version')
        self.dates[day] = _metadata(report)
        return report

    def commit(self) -> None:
        if not self.dates:
            raise ValueError('修订分支为空')
        atomic_json(self.store.root / 'branches' / f'{self.id}.json',
                    {'branch_id': self.id, 'dates': self.dates, 'input_ids': self.references,
                     'rule_version': self.rule_version, 'scope_version': self.scope_version,
                     'selection_policy_version': self.selection_policy_version, 'gaps': self.gaps, 'committed_at': cn_now().isoformat()})
        self.committed = True

    def close(self) -> None:
        if not self.committed and self.root.is_dir():
            shutil.rmtree(self.root)
