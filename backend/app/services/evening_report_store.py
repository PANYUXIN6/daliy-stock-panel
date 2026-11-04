"""Permanent archives, light read views and atomic revision-branch publication."""
from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from collections import Counter
from datetime import date
from pathlib import Path

from app.market_time import cn_now

_LOCK = threading.RLock()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text('utf-8'))
    except FileNotFoundError:
        return None


def report_view(report: dict) -> dict:
    """A response projection; complete evidence remains in the internal archive."""
    fields = ('date', 'trade_date', 'next_trade_date', 'cutoff', 'generated_at', 'version',
              'parent_version', 'branch_id', 'kind', 'mode', 'status', 'rule_version',
              'scope_version', 'selection_policy_version', 'disclaimer', 'summary', 'market', 'records', 'selected', 'limitations',
              'events', 'late_generation', 'recovery_note')
    result = {key: report[key] for key in fields if key in report}
    result['sectors'] = [{key: value for key, value in sector.items() if key not in ('scores', 'members', 'classification_members')}
                         for sector in report.get('sectors', [])]
    for sector in result['sectors']:
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
                            if key not in ('selection_path_audit', 'identity_continuity_audit')}
    provenance = report.get('provenance', {})
    classifications = []
    for item in provenance.get('classifications', []):
        classifications.append({key: value for key, value in item.items() if key not in ('symbols', 'memberships')}
                               | {'symbol_count': len(item.get('symbols', [])),
                                  'membership_count': len(item.get('memberships', []))})
    changes = []
    for item in provenance.get('membership_changes', []):
        changes.append({key: value for key, value in item.items() if key not in ('added', 'removed', 'first_observed')}
                       | {key + '_count': len(item.get(key, [])) for key in ('added', 'removed', 'first_observed')})
    result['provenance'] = {'classifications': classifications, 'membership_changes': changes,
                            'sector_filter': provenance.get('sector_filter', {}),
                            'suspension_evidence_count': len(provenance.get('suspensions', []))}
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
            ('version', 'kind', 'generated_at', 'parent_version', 'branch_id', 'rule_version', 'scope_version', 'selection_policy_version')} | {
                'input_id': report.get('input_reference', {}).get('input_id')}


class EveningReportStore:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / 'evening_report'

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
        suffix = '.view.json' if view else '.json'
        if entry.get('branch_id'):
            path = self.root / 'revisions' / entry['branch_id'] / f'{day}{suffix}'
        else:
            path = self.root / 'archive' / f"{entry['version']}{suffix}"
        return read_json(path)

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
                selected.append(original)
                if len(selected) == 7:
                    break
        return [read_json(self.root / 'archive' / f"{entry['version']}.state.json")
                for entry in reversed(selected)]

    def save(self, day: str, payload: dict, input_reference: dict) -> dict:
        with _LOCK:
            original = self.get(day)
            if original:
                return original
            report = {**payload, 'date': day, 'mode': 'live', 'status': 'published',
                      'version': uuid.uuid4().hex, 'kind': 'original', 'parent_version': None,
                      'generated_at': cn_now().isoformat(), 'input_reference': input_reference}
            path = self.root / 'archive' / report['version']
            atomic_json(path.with_suffix('.json'), report)
            atomic_json(path.with_suffix('.view.json'), report_view(report))
            atomic_json(path.with_suffix('.state.json'), history_view(report))
            # Readers see both complete archive and view only after this pointer.
            atomic_json(self._path(day), {'versions': [_metadata(report)]})
            return report

    def input_evidence(self, input_id: str) -> dict | None:
        if len(input_id) != 32 or any(c not in '0123456789abcdef' for c in input_id):
            raise ValueError('输入证据标识无效')
        return read_json(self.root / 'inputs' / f'{input_id}.json')

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
            path = self.root / 'inputs' / f"{evidence['input_id']}.json"
            existing = read_json(path)
            if existing is not None and existing != json.loads(json.dumps(evidence, default=str, allow_nan=False)):
                raise ValueError('既有输入证据不可覆写，请创建新的输入版本')
            if existing is None:
                atomic_json(path, evidence)
            pointer = {'date': day, 'complete': True, 'input_id': evidence['input_id']}
        else:
            pointer = evidence
        atomic_json(self.root / 'ready' / f'{date.fromisoformat(day)}.json', pointer)


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
        atomic_json(self.root / f'{date.fromisoformat(day)}.json', report)
        atomic_json(self.root / f'{date.fromisoformat(day)}.view.json', report_view(report))
        atomic_json(self.root / f'{date.fromisoformat(day)}.state.json', history_view(report))
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
