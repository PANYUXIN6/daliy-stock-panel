"""单日就绪记录与按交易日原子发布的晚报。"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import threading
import uuid
import zlib
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
    split_report,
)

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()


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
            before_commit()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict | None:
    opener = gzip.open if path.suffix == '.gz' else open
    try:
        with opener(path, 'rt', encoding='utf-8') as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError('预期 JSON 对象')
        return value
    except FileNotFoundError:
        return None
    except (OSError, EOFError, ValueError, zlib.error):
        logger.warning('晚报文件无法读取 %s', path, exc_info=True)
        return None


def report_view(report: dict) -> dict:
    """A response projection of permanent snapshots, excluding audit and state."""
    fields = ('date', 'trade_date', 'next_trade_date', 'cutoff', 'generated_at', 'version',
              'kind', 'mode', 'status', 'rule_version',
              'scope_version', 'selection_policy_version', 'disclaimer', 'summary', 'market', 'records', 'selected', 'limitations',
              'events', 'late_generation', 'recovery_note', 'storage_version', 'retention', 'as_of',
              'wave_rule', 'dip_rule_version', 'variable_definitions', 'industry_status')
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
                       | {key + '_count': item.get(key + '_count', len(item.get(key, []))) for key in ('added', 'removed')}
                       | {'first_observed_count': item.get('first_observed_count', len(item.get('first_observed_symbols', [])))})
    result['provenance'] = {'classifications': classifications, 'membership_changes': changes,
                            'recovery': provenance.get('recovery'),
                            'sector_filter': provenance.get('sector_filter', {}),
                            'suspension_evidence_count': provenance.get('suspension_evidence_count', len(provenance.get('suspensions', [])))}
    return result


class EveningReportStore:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir).resolve() / 'evening_report'

    def _path(self, day: str) -> Path:
        return self.root / 'reports' / f'{date.fromisoformat(day)}.json'

    def original_metadata(self, day: str, version: str | None = None) -> dict | None:
        pointer = read_json(self._path(day))
        if pointer is None:
            return None
        entries = pointer.get('versions')
        entry = entries[0] if isinstance(entries, list) and entries else None
        ref = entry.get('version') if isinstance(entry, dict) else None
        if not isinstance(ref, str) or not re.fullmatch('[a-f0-9]{32}', ref):
            logger.warning('晚报 %s 的归档标识不可读', day)
            return None
        return entry if version is None or version == ref else None

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

    def get(self, day: str, version: str | None = None, *, view: bool = False) -> dict | None:
        entry = self.original_metadata(day, version)
        if not entry:
            return None
        base = self._base(entry)
        if entry.get('storage_version') is None:
            # 首版曾分别写出原件与页面视图。任一原件缺失都不会触发重新发布。
            snapshot = base.with_suffix('.view.json')
            if view and snapshot.is_file():
                return read_json(snapshot)
            report = read_json(base.with_suffix('.json'))
        else:
            report = read_json(base.with_suffix('.json.gz'))
        if report is None:
            logger.warning('已发布的晚报归档缺失或不可读 %s %s', day, entry['version'])
            return None
        # 旧归档将数值存在 variables 中。只从请求的这一份文件取值。
        if report.get('storage_version') == 'evening-storage-v2':
            for row in report.get('records', []):
                obs = row.get('observation') or {}
                obs.setdefault('values', {k: v.get('value') for k, v in obs.get('variables', {}).items()})
        return report_view(report) if view else report

    def has_original(self, day: str) -> bool:
        return self.original_metadata(day) is not None

    def dates(self) -> list[dict]:
        result = []
        for path in sorted((self.root / 'reports').glob('*.json'), reverse=True):
            entry = self.original_metadata(path.stem)
            if entry:
                result.append({'date': path.stem, 'status': 'published', 'versions': [entry]})
        return result

    def save(self, day: str, payload: dict, input_reference: dict, *, before_publish=None) -> dict:
        with _LOCK:
            if self._path(day).exists():
                report = self.get(day)
                if report is None:
                    raise ValueError('已发布的晚报归档无法读取')
                return report
            report = {**payload, 'date': day, 'mode': 'live', 'status': 'published',
                      'storage_version': SCHEMA_VERSION, 'version': uuid.uuid4().hex, 'kind': 'original',
                      'generated_at': cn_now().isoformat(), 'input_reference': input_reference}
            permanent, audit, boundary = split_report(report)
            path = self._base(report)
            entry = {key: report.get(key) for key in ('version', 'kind', 'generated_at', 'rule_version',
                                                       'scope_version', 'selection_policy_version', 'storage_version')}
            files = [(path.with_suffix('.json.gz'), permanent), (path.with_suffix('.audit.json.gz'), audit),
                     (path.with_suffix('.candidates.json.gz'), boundary)]
            try:
                for destination, value in files:
                    atomic_json(destination, value)
                atomic_json(self._path(day), {'versions': [entry]}, before_commit=before_publish)
            except BaseException:
                for destination, _ in files:
                    destination.unlink(missing_ok=True)
                raise
            return permanent

    def readiness(self, day: str) -> dict | None:
        return read_json(self.root / 'ready' / f'{date.fromisoformat(day)}.json')

    def save_readiness(self, day: str, evidence: dict, *, before_commit=None) -> None:
        path = self.root / 'ready' / f'{date.fromisoformat(day)}.json'
        atomic_json(path, {**evidence, 'date': day}, before_commit=before_commit)

    def save_diagnostic(self, report: dict, *, reason: str) -> None:
        """Only explicit per-run diagnostics retain full intermediate results."""
        now = cn_now()
        atomic_json(self.root / 'diagnostics' / f'{now.date()}-{uuid.uuid4().hex}.json.gz', {
            'created_at': now.isoformat(), 'expires_at': (now + timedelta(days=DIAGNOSTIC_RETENTION_DAYS)).isoformat(),
            'trigger': reason, 'report': report})

    def prune(self, today: date, cancel_check=None) -> dict:
        check = cancel_check or (lambda: None)
        if self.root.resolve() != self.root:
            raise ValueError('晚报维护目录不能是符号链接')
        targets = []
        audit_cutoff = (today - timedelta(days=AUDIT_RETENTION_DAYS)).isoformat()
        for item in self.dates():
            if item['date'] < audit_cutoff:
                base = self._base(item['versions'][0])
                targets.extend(base.with_suffix(suffix) for suffix in ('.audit.json.gz', '.candidates.json.gz'))
        for folder, pattern, ttl in (('ready', '*.json', INPUT_RETENTION_DAYS),
                                     ('attempts', '*.json', ATTEMPT_RETENTION_DAYS),
                                     ('diagnostics', '*.json.gz', DIAGNOSTIC_RETENTION_DAYS)):
            cutoff = (today - timedelta(days=ttl)).isoformat()
            targets.extend(path for path in (self.root / folder).glob(pattern) if path.name[:10] < cutoff)
        count = size = 0
        for path in targets:
            check()
            if not path.exists():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                raise ValueError('晚报维护目标超出模块目录')
            size += path.stat().st_size
            path.unlink()
            count += 1
        return {'removed_files': count, 'removed_bytes': size}
