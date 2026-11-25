"""Shared recommendation history and account-owned, reversible feedback.

SQLite transactions keep an event and its current state indivisible across web
workers. Read requests never create a database or calculate market data. History
is indexed by recommendation month; no report or price history is copied here.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.market_time import cn_now
from app.services.evening_report_start_study import published_on_trade_date
from app.services.evening_report_store import EveningReportStore
from app.services.evening_report_wave_context import project_context_refs
from app.services.private_storage import private_path
from app.services.trading_day import calendar_status
from app.services.user_context import current_user

EVALUATION_VERSION = 'close-forward-qfq-v3'
ANALYSIS_VERSION = 'audit-missingness-v1'
_PENDING_EVALUATIONS = ' OR '.join(
    f"""json_extract(performance, '$."{window}".status') IS NOT 'evaluated'
        OR json_extract(performance, '$."{window}".path.missing_count') > 0"""
    for window in (1, 3, 5)
)


class FeedbackConflictError(ValueError):
    pass


def _shared_path(data_dir: Path) -> Path:
    return Path(data_dir) / 'evening_report' / 'tracking.sqlite3'


def _personal_path(data_dir: Path) -> Path:
    return private_path(data_dir, 'evening_report', 'records.sqlite3')


@contextmanager
def _database(path: Path, *, write: bool = False):
    if not write and not path.exists():
        yield None
        return
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path if write else path.as_uri() + '?mode=ro', uri=not write, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        if not write and not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
            yield None
            return
        if write:
            db.execute('BEGIN IMMEDIATE')
        yield db
        if write:
            db.commit()
    except BaseException:
        if write:
            db.rollback()
        raise
    finally:
        db.close()


def _schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS refresh_runs(request_key TEXT PRIMARY KEY, completed_at TEXT NOT NULL)')
    db.execute("CREATE TABLE IF NOT EXISTS reports(version TEXT PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL, audit_feature_keys TEXT NOT NULL DEFAULT '[]', audit_sample_count INTEGER, forward_sample INTEGER NOT NULL)")
    db.execute('''CREATE TABLE IF NOT EXISTS episodes(
        id TEXT PRIMARY KEY, symbol TEXT NOT NULL, first_reference TEXT NOT NULL,
        started_at TEXT NOT NULL, last_seen TEXT NOT NULL, absence_count INTEGER NOT NULL,
        ended_at TEXT, historical INTEGER NOT NULL DEFAULT 0,
        end_known_at TEXT, end_reason TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS recommendations(
        id TEXT PRIMARY KEY, version TEXT NOT NULL, day TEXT NOT NULL, month TEXT NOT NULL,
        symbol TEXT NOT NULL, name TEXT, episode_id TEXT NOT NULL, observation TEXT NOT NULL,
        appearance_seq INTEGER NOT NULL,
        absence_count_at_recommendation INTEGER)''')
    db.execute('CREATE INDEX IF NOT EXISTS recommendations_month ON recommendations(month, day)')
    db.execute('CREATE INDEX IF NOT EXISTS recommendations_episode ON recommendations(episode_id)')
    db.execute('''CREATE TABLE IF NOT EXISTS outcome_evaluations(
        day TEXT NOT NULL, symbol TEXT NOT NULL, performance TEXT NOT NULL,
        PRIMARY KEY(day,symbol))''')
    db.execute(f'CREATE INDEX IF NOT EXISTS outcome_pending_paths ON outcome_evaluations(day,symbol) WHERE {_PENDING_EVALUATIONS}')
    db.execute('''CREATE TABLE IF NOT EXISTS audit_cohorts(
        id TEXT PRIMARY KEY, version TEXT NOT NULL, day TEXT NOT NULL, symbol TEXT NOT NULL,
        selected INTEGER NOT NULL, comparable INTEGER NOT NULL, missing_features TEXT NOT NULL,
        comparison_group TEXT)''')
    db.execute('CREATE INDEX IF NOT EXISTS audit_cohorts_version ON audit_cohorts(version)')
    db.execute('''CREATE TABLE IF NOT EXISTS audit_analyses(
        version TEXT NOT NULL, rule_version TEXT NOT NULL, data_version TEXT NOT NULL,
        evaluated_at TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(version,rule_version,data_version))''')


def _personal_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS feedback(
        episode_id TEXT PRIMARY KEY, starred INTEGER NOT NULL, outcome TEXT,
        revision INTEGER NOT NULL, updated_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS events(
        request_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, reference_id TEXT NOT NULL,
        month TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL,
        actions TEXT NOT NULL, revision INTEGER NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS events_month ON events(month, at)')


def _selected(report: dict) -> list[dict]:
    return [row for row in report.get('records', []) if row.get('selected') is True]


def _register(db, report: dict, calendar: list[str], *, audit_rows: list[dict] | None = None) -> None:
    version = report['version']
    if db.execute('SELECT 1 FROM reports WHERE version=?', (version,)).fetchone():
        return
    day = report.get('trade_date') or report['date']
    kind = report.get('kind', 'original')
    last = db.execute('SELECT MAX(day) FROM reports').fetchone()[0]
    historical = bool(last and day <= last)
    rows = _selected(report)
    symbols = {row['symbol'] for row in rows}
    if not historical:
        prior_days = [d for d in calendar if d < day]
        consecutive = bool(last and prior_days and prior_days[-1] == last)
        if not consecutive:
            # Missing / failed reports break the five-report absence sequence.
            db.execute('UPDATE episodes SET absence_count=0 WHERE ended_at IS NULL AND historical=0')
        for episode in db.execute('SELECT * FROM episodes WHERE ended_at IS NULL AND historical=0').fetchall():
            if episode['symbol'] not in symbols:
                absent = episode['absence_count'] + 1
                ended = absent >= 5
                db.execute('UPDATE episodes SET absence_count=?,ended_at=?,end_known_at=?,end_reason=? WHERE id=?',
                           (absent, day if ended else None, cn_now().isoformat() if ended else None,
                            'five_consecutive_valid_reports_absent' if ended else None, episode['id']))
    for row in rows:
        symbol = row['symbol']
        reference = f'{version}:{symbol}'
        episode = None
        if not historical:
            active = db.execute('SELECT id FROM episodes WHERE symbol=? AND ended_at IS NULL AND historical=0',
                                (symbol,)).fetchone()
            episode = active['id'] if active else None
        if not episode:
            episode = reference
            db.execute('''INSERT INTO episodes(id,symbol,first_reference,started_at,last_seen,absence_count,ended_at,historical)
                          VALUES(?,?,?,?,?,0,NULL,?)''',
                       (episode, symbol, reference, day, day, int(historical)))
        elif not historical:
            db.execute('UPDATE episodes SET last_seen=?,absence_count=0 WHERE id=?', (day, episode))
        appearance = 1 + db.execute('SELECT COUNT(*) FROM recommendations WHERE episode_id=?',
                                    (episode,)).fetchone()[0]
        observation = row.get('observation') or {}
        db.execute('''INSERT INTO recommendations(id,version,day,month,symbol,name,episode_id,observation,
                      appearance_seq,absence_count_at_recommendation)
                      VALUES(?,?,?,?,?,?,?,?,?,?)''',
                   (reference, version, day, day[:7], symbol, row.get('name'), episode,
                    json.dumps(observation, ensure_ascii=False, allow_nan=False), appearance, 0))
    # Selected and comparable features live in the permanent snapshot. Audit-only
    # securities have compact decisions and never acquire a personal identity.
    if report.get('audit'):
        if not audit_rows:
            # Long-term candidate snapshots remain available after the audit retention period.
            audit_rows = [{**row, 'decision': row.get('observation', {}).get('decision', {}),
                           'missing_features': row.get('observation', {}).get('data_completeness', {}).get('missing_fields', [])}
                          for row in report.get('records', [])]
        for row in audit_rows:
            decision = row['decision']
            db.execute('INSERT OR IGNORE INTO audit_cohorts VALUES(?,?,?,?,?,?,?,?)',
                       (f'audit:{version}:{row["symbol"]}', version, day, row['symbol'], int(row['selected']),
                        int(decision.get('comparison_eligible', False)),
                        json.dumps(row['missing_features']),
                        json.dumps(decision.get('comparison_group'))))
    db.execute('INSERT INTO reports(version,day,kind,audit_feature_keys,audit_sample_count,forward_sample) VALUES(?,?,?,?,?,?)',
               (version, day, kind, json.dumps(report.get('audit', {}).get('feature_keys', [])),
                report.get('audit', {}).get('same_cycle_sample_count'),
                int(published_on_trade_date(report | {'trade_date': day}))))


def _valid_price(row: dict | None) -> bool:
    return bool(row and row.get('formal') is True and isinstance(row.get('close'), (float, int))
                and math.isfinite(row['close']) and row['close'] > 0)


def evaluation_prices(frame: pl.DataFrame, symbols: set[str], days: set[str]) -> dict:
    fields = ['symbol', 'date', 'close', 'formal', 'raw_close', 'raw_high', 'raw_low',
              'volume', 'amount', 'is_st', 'signal_limit_up', 'signal_limit_down']
    rows = (frame.filter(pl.col('symbol').is_in(symbols) & pl.col('date').cast(pl.String).is_in(days))
            .select([column for column in fields if column in frame.columns]))
    return {(row['symbol'], str(row['date'])): row for row in rows.iter_rows(named=True)}


def evaluation_data_version(inputs) -> dict:
    return {'input_id': inputs.provenance.get('input_id'),
            'stock_generation': inputs.provenance.get('input_versions', {}).get('stock_generation'),
            'cutoff': inputs.cutoff,
            'adjustment_source': inputs.provenance.get('evaluation_context', {}).get('adjustment_source')}


def evaluate_performance(symbol: str, day: str, *, calendar: list[str], as_of: str,
                         prices: dict[tuple[str, str], dict], suspensions: set[tuple[str, str]],
                         data_version: dict, evaluation_context: dict | None = None) -> dict:
    """One adjusted price snapshot for both endpoints; no shifted target dates."""
    results = {}
    context = evaluation_context or {}
    evaluated_at = cn_now().isoformat()
    position = calendar.index(day) if day in calendar else None
    for window in (1, 3, 5):
        target = (calendar[position + window] if position is not None and position + window < len(calendar) else None)
        value = {'window': window, 'target_date': target, 'as_of': as_of,
                 'return': None, 'status': 'not_due', 'reason': '尚未到期或交易日历尚未覆盖目标日',
                 'price_basis': 'same_snapshot_adjusted_close', 'data_version': data_version,
                 'trading_constraints': [], 'reason_code': 'not_due',
                 'evaluated_at': evaluated_at, 'evaluation_rule_version': EVALUATION_VERSION,
                 'return_unit': 'ratio', 'adjust_method': context.get('adjust_method', 'same_snapshot_adjusted_close'),
                 'adjust_base_date': context.get('adjust_base_date'),
                 'adjust_rule_version': context.get('adjust_rule_version'),
                 'calendar_version': context.get('calendar_version'),
                 'suspension_source': context.get('suspension_source', []),
                 'constraint_coverage': {},
                 'd_close_raw': None, 'target_close_raw': None,
                 'd_close_adjusted': None, 'target_close_adjusted': None,
                 'd_adjustment_multiplier': None, 'target_adjustment_multiplier': None}
        start = prices.get((symbol, day)) if day <= as_of else None
        end = prices.get((symbol, target)) if target and target <= as_of else None
        constraints = []
        for label, endpoint_day, row in (('推荐日', day, start), ('目标日', target, end)):
            if endpoint_day is None or endpoint_day > as_of:
                continue
            if (symbol, endpoint_day) in suspensions:
                constraints.append(label + '停牌')
            if row:
                for key, text in (('signal_limit_up', '涨停'), ('signal_limit_down', '跌停'), ('is_st', 'ST')):
                    if row.get(key):
                        constraints.append(label + text)
                high, low = row.get('raw_high'), row.get('raw_low')
                if isinstance(high, (float, int)) and high > 0 and high == low:
                    constraints.append(label + '一字价格')
                if row.get('volume') == 0 or row.get('amount') == 0:
                    constraints.append(label + '无成交')
            value['constraint_coverage'][endpoint_day] = {
                'st_status': 'known' if row and row.get('is_st') is not None else 'unknown',
                'st_unknown_reason': None if row and row.get('is_st') is not None else '缺少该交易日证券资格快照',
                'limit_up_status': 'known' if row and row.get('signal_limit_up') is not None else 'unknown',
                'limit_down_status': 'known' if row and row.get('signal_limit_down') is not None else 'unknown',
                'source_version': data_version}
        value['trading_constraints'] = constraints
        for prefix, row in (('d', start), ('target', end)):
            for suffix, key in (('raw', 'raw_close'), ('adjusted', 'close')):
                price = row.get(key) if row else None
                value[f'{prefix}_close_{suffix}'] = (price if isinstance(price, (float, int))
                                                    and math.isfinite(price) and price > 0 else None)
            raw = value[f'{prefix}_close_raw']
            adjusted = value[f'{prefix}_close_adjusted']
            if raw is not None and adjusted is not None:
                value[f'{prefix}_adjustment_multiplier'] = adjusted / raw
        if position is None:
            value.update(status='unavailable', reason='推荐日不在已确认交易日历中', reason_code='calendar_unconfirmed')
        elif target and target <= as_of:
            if (symbol, day) in suspensions or (symbol, target) in suspensions:
                value.update(status='unavailable', reason='评价端点停牌，不顺延', reason_code='suspended_endpoint')
            elif not _valid_price(start) or not _valid_price(end):
                value.update(status='unavailable', reason='评价端点缺少可信正式价格',
                             reason_code='missing_endpoint' if start is None or end is None else 'untrusted_endpoint')
            else:
                value.update(status='evaluated', reason='推荐后价格表现，不代表实际交易收益',
                             reason_code='valid_price_endpoints',
                             **{'return': end['close'] / start['close'] - 1},
                             trading_constraints=constraints)
        if target and target <= as_of:
            days = calendar[position + 1:position + window + 1]
            halted = [item for item in days if (symbol, item) in suspensions]
            observed = [prices.get((symbol, item)) for item in days if item not in halted]
            missing = sum(not _valid_price(row) for row in observed)
            path_complete = _valid_price(start) and not missing and (symbol, day) not in suspensions
            returns = [0., *(row['close'] / start['close'] - 1 for row in observed)] if path_complete else []
            value['path'] = {'status': 'evaluated' if path_complete else 'unavailable',
                             'max_favorable_return': max(returns) if returns else None,
                             'max_adverse_return': min(returns) if returns else None,
                             'expected_count': len(days), 'valid_count': len(observed) - missing,
                             'missing_count': missing, 'suspended_count': len(halted),
                             'as_of': as_of, 'data_version': data_version, 'adjust_base_date': value['adjust_base_date']}
        results[str(window)] = value
    return results


def refresh_tracking(data_dir: Path, inputs, cancel_check=None, *, evaluate=True) -> dict:
    """Run after publication, with accepted local inputs. Failure never unpublishes."""
    store = EveningReportStore(data_dir)
    path = _shared_path(data_dir)
    check = cancel_check or (lambda: None)
    check()
    with _database(path) as db:
        known = {row[0] for row in db.execute('SELECT version FROM reports')} if db else set()
    reports = []
    audits = {}
    for entry in reversed(store.dates()):
        check()
        if entry['date'] > inputs.trade_date:
            continue
        version = entry['versions'][0]['version']
        if version not in known:
            report = store.get(entry['date'], version)
            if report and report.get('status') == 'published':
                reports.append(report)
                audits[version] = store.audit_rows(entry['date'], version)
    context = inputs.provenance.get('evaluation_context', {})
    calendar = sorted(set(inputs.provenance.get('calendar', {}).get('days', inputs.calendar)))
    suspensions = {(row['symbol'], str(row['date'])) for row in inputs.provenance.get('suspensions', [])}
    # Computation happens outside the write transaction; the second read merges
    # with another collector's already completed evaluations.
    with _database(path, write=True) as db:
        _schema(db)
        for report in reports:
            check()
            _register(db, report, calendar, audit_rows=audits[report['version']])
        db.execute('''INSERT OR IGNORE INTO outcome_evaluations(day,symbol,performance)
                      SELECT day,symbol,'{}' FROM recommendations UNION
                      SELECT day,symbol,'{}' FROM audit_cohorts WHERE comparable=1''')
        if not evaluate:
            return {'registered_reports': len(reports), 'evaluated_recommendations': 0}
        pending = db.execute(f'SELECT * FROM outcome_evaluations WHERE ({_PENDING_EVALUATIONS}) AND day<=?',
                             (inputs.trade_date,)).fetchall()
    version = evaluation_data_version(inputs)
    needed_days = set()
    for row in pending:
        check()
        if row['day'] in calendar:
            index = calendar.index(row['day'])
            needed_days.update(calendar[index:index + 6])
    prices = evaluation_prices(inputs.prices, {row['symbol'] for row in pending}, needed_days)
    evaluations = {}
    for row in pending:
        check()
        previous = json.loads(row['performance'])
        fresh = evaluate_performance(row['symbol'], row['day'], calendar=calendar, as_of=inputs.trade_date,
                                     prices=prices, suspensions=suspensions, data_version=version,
                                     evaluation_context=context)
        evaluations[(row['day'], row['symbol'])] = _merge_performance(previous, fresh, inputs.trade_date)
    with _database(path, write=True) as db:
        check()
        for (day, symbol), fresh in evaluations.items():
            current = json.loads(db.execute('SELECT performance FROM outcome_evaluations WHERE day=? AND symbol=?', (day, symbol)).fetchone()[0])
            fresh = _merge_performance(current, fresh, inputs.trade_date)
            db.execute('UPDATE outcome_evaluations SET performance=? WHERE day=? AND symbol=?',
                       (json.dumps(fresh, ensure_ascii=False, allow_nan=False, default=str), day, symbol))
        missing_analyses = {row[0] for row in db.execute(
            """SELECT r.day FROM reports r WHERE r.day<=?
               AND EXISTS (SELECT 1 FROM audit_cohorts a WHERE a.version=r.version)
               AND NOT EXISTS (SELECT 1 FROM audit_analyses h WHERE h.version=r.version AND h.rule_version=?)""",
            (inputs.trade_date, ANALYSIS_VERSION))}
    affected_days = missing_analyses | {day for day, _symbol in evaluations}
    _save_audit_analyses(path, version, inputs.trade_date, affected_days, check)
    return {'registered_reports': len(reports), 'evaluated_recommendations': len(evaluations)}


def _merge_performance(previous: dict, fresh: dict, as_of: str) -> dict:
    for key, value in previous.items():
        if value.get('status') == 'evaluated' or value.get('as_of', '') > as_of:
            current = fresh.get(key, {})
            path, updated = value.get('path', {}), current.get('path', {})
            if (value.get('status') == current.get('status') == 'evaluated'
                    and value['target_date'] == current['target_date']
                    and updated and path and path['missing_count'] > updated['missing_count']
                    and path.get('as_of', value['as_of']) <= updated.get('as_of', current['as_of'])):
                # Freeze the accepted endpoint. Only the incomplete path acquires later evidence.
                value = {**value, 'path': updated}
            fresh[key] = value
            continue
        current = fresh.get(key)
        if current is None:
            continue
        # Keep dated qualification facts recorded while that endpoint was
        # current. Prices always come from the newly rebased pair above.
        for day, evidence in value.get('constraint_coverage', {}).items():
            coverage = current.get('constraint_coverage', {}).get(day)
            if coverage is None:
                continue
            label = '目标日' if day == current['target_date'] else '推荐日'
            for field, suffix in (('st_status', 'ST'), ('limit_up_status', '涨停'), ('limit_down_status', '跌停')):
                if coverage[field] == 'unknown' and evidence.get(field) == 'known':
                    coverage[field] = 'known'
                    coverage.setdefault('retained_source_versions', {})[field] = evidence.get('retained_source_versions', {}).get(field, evidence.get('source_version'))
                    if field == 'st_status':
                        coverage['st_unknown_reason'] = None
                    flag = label + suffix
                    if flag in value.get('trading_constraints', []) and flag not in current['trading_constraints']:
                        current['trading_constraints'].append(flag)
    return fresh


def _save_audit_analyses(path: Path, data_version: dict, as_of: str, days: set[str], check) -> None:
    from app.services.evening_report_audit import COHORT_VERSION, NON_INDEPENDENT_NOTE, missingness

    if not days:
        return
    with _database(path) as db:
        rows = db.execute(f'''SELECT a.*,o.performance,r.audit_feature_keys,r.audit_sample_count,r.kind FROM audit_cohorts a
                              LEFT JOIN outcome_evaluations o ON a.day=o.day AND a.symbol=o.symbol
                              JOIN reports r ON r.version=a.version
                              WHERE a.day IN ({','.join('?' for _ in days)})''', sorted(days)).fetchall()
    versions = defaultdict(list)
    for row in rows:
        versions[row['version']].append({**dict(row), 'missing_features': json.loads(row['missing_features']),
                                        'performance': json.loads(row['performance'] or '{}')})
    analyses = []
    for version, samples in versions.items():
        check()
        comparable = [row for row in samples if row['comparable']]
        keys = sorted(set(json.loads(samples[0]['audit_feature_keys'])))
        windows = {}
        for window in ('1', '3', '5'):
            evaluated = [row for row in comparable if row['performance'].get(window, {}).get('status') == 'evaluated']
            unavailable = [row for row in comparable if row['performance'].get(window, {}).get('status') == 'unavailable']
            windows[window] = {
                'evaluated': len(evaluated), 'unavailable': len(unavailable),
                'not_due': len(comparable) - len(evaluated) - len(unavailable),
                'missingness': missingness({'evaluated': evaluated, 'unavailable': unavailable}, keys)}
        complete_audit = samples[0]['audit_sample_count'] in (None, len(samples))
        payload = {
            'as_of': as_of, 'rule_version': ANALYSIS_VERSION, 'cohort_definition_version': COHORT_VERSION,
            'report_version': version,
            'report_kind': samples[0]['kind'],
            'same_cycle_sample_count': len(samples), 'comparable_sample_count': len(comparable),
            'distinct_trade_dates': 1, 'non_independent_notice': NON_INDEPENDENT_NOTE,
            'missingness': missingness({'selected': [row for row in comparable if row['selected']],
                                       'comparable_unselected': [row for row in comparable if not row['selected']]}, keys),
            'audit_scope': 'actually_evaluated_securities', 'audit_retention_complete': complete_audit,
            'audit_missingness': missingness({'selected': [row for row in samples if row['selected']],
                                             'all_unselected': [row for row in samples if not row['selected']]}, keys) if complete_audit else None,
            'windows': windows,
            'comparison_groups': dict(Counter(row['comparison_group'] for row in comparable)),
            'significance': {'status': 'not_tested', 'reason': '同周期样本不独立；报告缺失率与差值，不伪造显著性结论'},
        }
        analyses.append((version, ANALYSIS_VERSION, json.dumps(data_version, sort_keys=True),
                         cn_now().isoformat(), json.dumps(payload, ensure_ascii=False, allow_nan=False)))
    with _database(path, write=True) as db:
        check()
        db.executemany('INSERT OR IGNORE INTO audit_analyses VALUES(?,?,?,?,?)', analyses)


def audit_history(data_dir: Path, version: str) -> dict:
    """Read-only audit export; candidate IDs cannot be used as feedback IDs."""
    with _database(_shared_path(data_dir)) as db:
        if not db:
            return {'records': [], 'analyses': []}
        rows = [dict(row) for row in db.execute('''SELECT a.*,o.performance FROM audit_cohorts a
                    LEFT JOIN outcome_evaluations o ON a.day=o.day AND a.symbol=o.symbol
                    WHERE a.version=? ORDER BY a.symbol''', (version,))]
        analyses = [dict(row) for row in db.execute('SELECT * FROM audit_analyses WHERE version=? ORDER BY evaluated_at', (version,))]
    for row in rows:
        for field in ('performance', 'missing_features', 'comparison_group'):
            row[field] = json.loads(row[field]) if row[field] else None
        if row['performance'] is None:
            row['performance'] = {}
    for analysis in analyses:
        analysis['payload'] = json.loads(analysis['payload'])
        analysis['data_version'] = json.loads(analysis['data_version'])
    return {'records': rows, 'analyses': analyses}


def _feedback(row=None) -> dict:
    return {'starred': bool(row['starred']) if row else False,
            'outcome': row['outcome'] if row else None,
            'revision': row['revision'] if row else 0,
            'updated_at': row['updated_at'] if row else None}


def _all_feedback(data_dir: Path) -> dict:
    with _database(_personal_path(data_dir)) as db:
        return {row['episode_id']: _feedback(row) for row in db.execute('SELECT * FROM feedback')} if db else {}


def _project_performance(performance: dict, calendar: list[str], position: int | None,
                         closed_through: str) -> dict:
    projected = dict(performance)
    for window in (1, 3, 5):
        key = str(window)
        value = performance.get(key, {})
        calendar_unconfirmed = value.get('status') == 'unavailable' and value.get('reason_code') == 'calendar_unconfirmed'
        if value and value['status'] != 'not_due' and not calendar_unconfirmed:
            continue
        target = value.get('target_date')
        if target is None and position is not None and position + window < len(calendar):
            target = calendar[position + window]
        if target is None and position is None and (calendar or calendar_unconfirmed):
            projected[key] = {'window': window, 'as_of': None, 'return': None, 'trading_constraints': [],
                              'data_version': None, **value, 'target_date': None, 'status': 'unavailable',
                              'reason_code': 'calendar_unconfirmed', 'reason': '推荐日不在已确认交易日历中'}
            continue
        as_of = value.get('as_of')
        pending = bool(target and target <= closed_through)
        status = 'overdue_pending_review' if pending else 'not_due'
        reason = '目标交易日待确认' if target is None else '目标日尚未收盘'
        if pending:
            reason = f'最近评价截至 {as_of}' if as_of else '尚无回看结果'
        projected[key] = {'window': window, 'as_of': None, 'return': None, 'trading_constraints': [], 'data_version': None,
                          **value, 'target_date': target, 'status': status, 'reason_code': status, 'reason': reason}
    return projected


def _recommendations(data_dir: Path, *, day: str | None = None, version: str | None = None,
                     month: str | None = None) -> list[dict]:
    clauses, params = [], []
    for key, value in (('r.day', day), ('r.version', version), ('r.month', month)):
        if value:
            clauses.append(key + '=?')
            params.append(value)
    with _database(_shared_path(data_dir)) as db:
        rows = []
        if db:
            sql = """SELECT r.*,e.started_at,e.last_seen,e.ended_at,e.first_reference,e.historical,
                            p.kind AS report_kind,p.forward_sample,e.end_known_at,e.end_reason,
                            COALESCE(o.performance,'{}') AS performance
                     FROM recommendations r JOIN episodes e ON r.episode_id=e.id
                     JOIN reports p ON p.version=r.version
                     LEFT JOIN outcome_evaluations o ON r.day=o.day AND r.symbol=o.symbol"""
            if clauses:
                sql += ' WHERE ' + ' AND '.join(clauses)
            sql += ' ORDER BY r.day DESC,r.rowid'
            rows = [dict(row) for row in db.execute(sql, params)]
    feedback = _all_feedback(data_dir)
    calendar = calendar_status(data_dir).get('days', []) if rows else []
    positions = {value: index for index, value in enumerate(calendar)}
    now = cn_now()
    closed_through = (now.date() - timedelta(days=now.hour < 15)).isoformat()
    for row in rows:
        row['observation'] = json.loads(row['observation'])
        row['performance'] = _project_performance(json.loads(row['performance']), calendar,
                                                 positions.get(row['day']), closed_through)
        row['feedback'] = feedback.get(row['episode_id'], _feedback())
        row['count_key'] = [current_user().id, row['symbol'], row['episode_id'], row['day']]
    return rows


def personal_report(data_dir: Path, day: str, version: str) -> dict:
    user = current_user()
    rows = _recommendations(data_dir, day=day, version=version)
    with _database(_shared_path(data_dir)) as db:
        ready = bool(db and db.execute('SELECT 1 FROM reports WHERE version=? AND day=?', (version, day)).fetchone())
    view = {'user_id': user.id, 'date': day, 'version': version, 'ready': ready, 'records': rows}
    return project_context_refs(view, EveningReportStore(data_dir))


def update_feedback(data_dir: Path, *, day: str, version: str, symbol: str,
                    starred: bool, outcome: str | None, expected_revision: int, request_id: str) -> dict:
    report = EveningReportStore(data_dir).get(day, version, view=True)
    if not report or symbol not in {row['symbol'] for row in _selected(report)}:
        raise ValueError('只能记录该报告中已入选的股票')
    reference = f'{version}:{symbol}'
    with _database(_shared_path(data_dir)) as db:
        row = db.execute('SELECT episode_id FROM recommendations WHERE id=?', (reference,)).fetchone() if db else None
    if row is None:
        raise FeedbackConflictError('观察历史正在准备，请稍后刷新重试')
    episode = row['episode_id']
    if outcome not in (None, 'profit', 'loss') or not isinstance(starred, bool):
        raise ValueError('反馈状态无效')
    payload = json.dumps({'reference_id': reference, 'starred': starred, 'outcome': outcome,
                          'expected_revision': expected_revision}, sort_keys=True)
    with _database(_personal_path(data_dir), write=True) as db:
        _personal_schema(db)
        existing = db.execute('SELECT payload FROM events WHERE request_id=?', (request_id,)).fetchone()
        current = _feedback(db.execute('SELECT * FROM feedback WHERE episode_id=?', (episode,)).fetchone())
        if existing:
            if existing['payload'] != payload:
                raise FeedbackConflictError('重复请求标识对应不同修改，请刷新后重试')
            return {'episode_id': episode, **current}
        if current['revision'] != expected_revision:
            raise FeedbackConflictError('记录已在其他页面更新，请刷新后重试')
        actions = []
        if current['starred'] != starred:
            actions.append('star_set' if starred else 'star_cancel')
        if current['outcome'] != outcome:
            if current['outcome']:
                actions.append(current['outcome'] + '_cancel')
            if outcome:
                actions.append(outcome + '_set')
        now = cn_now().isoformat()
        revision = current['revision'] + bool(actions)
        db.execute('INSERT OR REPLACE INTO feedback VALUES(?,?,?,?,?)',
                   (episode, int(starred), outcome, revision, now))
        db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)',
                   (request_id, episode, reference, now[:7], now, payload, json.dumps(actions), revision))
        return {'episode_id': episode, 'starred': starred, 'outcome': outcome,
                'revision': revision, 'updated_at': now}


def personal_history(data_dir: Path, month: str | None = None) -> dict:
    user = current_user()
    rows = _recommendations(data_dir, month=month)
    episodes = {row['episode_id'] for row in rows}
    with _database(_personal_path(data_dir)) as db:
        events = [dict(row) for row in db.execute('SELECT * FROM events ORDER BY at DESC')] if db else []
    for event in events:
        event['actions'] = json.loads(event['actions'])
        event.pop('payload')
    view = {'user_id': user.id, 'records': rows, 'events': [e for e in events if e['episode_id'] in episodes]}
    return project_context_refs(view, EveningReportStore(data_dir))


def personal_overview(data_dir: Path) -> dict:
    user = current_user()
    rows = _recommendations(data_dir)
    episodes = {row['episode_id']: row for row in rows if row['id'] == row['first_reference']}
    states = [row['feedback'] for row in episodes.values()]
    forward = [row for row in episodes.values() if row['forward_sample']]
    performance = {}
    for window in ('1', '3', '5'):
        samples = [row['performance'][window] for row in forward]
        counts = Counter(row['status'] for row in samples)
        valid = [row['return'] for row in samples if row['status'] == 'evaluated']
        performance[window] = {'total': len(samples), 'evaluated': counts['evaluated'],
                               'not_due': counts['not_due'], 'overdue_pending_review': counts['overdue_pending_review'],
                               'unavailable': counts['unavailable'],
                               'mean_return': sum(valid) / len(valid) if valid else None}
    return {'user_id': user.id, 'episode_count': len(episodes),
            'recommendation_count': len(rows),
            'starred': sum(row['starred'] for row in states),
            'unstarred': sum(not row['starred'] for row in states),
            'profit': sum(row['outcome'] == 'profit' for row in states),
            'loss': sum(row['outcome'] == 'loss' for row in states),
            'unmarked': sum(row['outcome'] is None for row in states), 'performance': performance}


def refresh_completed(data_dir: Path, request_key: str) -> bool:
    with _database(_shared_path(data_dir)) as db:
        return bool(db and db.execute('SELECT 1 FROM refresh_runs WHERE request_key=?', (request_key,)).fetchone())


def prune_tracking(data_dir: Path, today: date, cancel_check=None) -> dict:
    """Expire audit-only tracking rows after their publication retention period."""
    from app.services.evening_report_archive import (
        ATTEMPT_RETENTION_DAYS,
        AUDIT_RETENTION_DAYS,
    )

    check = cancel_check or (lambda: None)
    check()
    path = _shared_path(data_dir)
    if not path.exists():
        return {'removed_audit_rows': 0}
    if path.is_symlink() or path.resolve().parent != (Path(data_dir) / 'evening_report').resolve():
        raise ValueError('晚报维护数据库超出模块目录')
    cutoff = (today - timedelta(days=AUDIT_RETENTION_DAYS)).isoformat()
    expired_versions = [(day['versions'][0]['version'],) for day in EveningReportStore(data_dir).dates()
                        if day['date'] < cutoff]
    with _database(path, write=True) as db:
        check()
        _schema(db)
        removed = db.executemany('DELETE FROM audit_cohorts WHERE version=? AND selected=0 AND comparable=0',
                                 expired_versions).rowcount
        db.executemany('DELETE FROM audit_analyses WHERE version=?', expired_versions)
        db.execute('DELETE FROM refresh_runs WHERE completed_at<?',
                   ((today - timedelta(days=ATTEMPT_RETENTION_DAYS)).isoformat(),))
        check()  # Cancellation rolls back the database transaction before commit.
    return {'removed_audit_rows': removed}


def refresh_after_publication(data_dir: Path, day: str, cancel_check=None, request_key=None) -> dict:
    from types import SimpleNamespace

    from app.services.daily_coverage import DailyCoverageError
    from app.services.evening_report_inputs import (
        load_inputs,
        prepare_evaluation_inputs,
        require_ready,
    )
    target = date.fromisoformat(day)
    warning = None
    try:
        evidence = require_ready(data_dir, target)
        inputs = load_inputs(data_dir, target, evidence, cancel_check)
        prepare_evaluation_inputs(inputs)
        require_ready(data_dir, target, input_id=evidence['input_id'])
    except DailyCoverageError as exc:
        # Published recommendations remain recordable even if price inputs are
        # currently unavailable. Only objective evaluation waits for new inputs.
        warning = str(exc)
        inputs = SimpleNamespace(calendar=calendar_status(data_dir).get('days', []),
                                 trade_date=day, provenance={})
    result = refresh_tracking(data_dir, inputs, cancel_check, evaluate=warning is None)
    from app.services.shared_runtime import publish_data_update

    # Failed notification writes must remain eligible for the scheduled retry.
    publish_data_update(data_dir, ['evening_report_tracking'])
    if request_key and (warning is None or request_key.endswith(':unready')):
        with _database(_shared_path(data_dir), write=True) as db:
            db.execute('INSERT OR REPLACE INTO refresh_runs VALUES(?,?)', (request_key, cn_now().isoformat()))
    return {**result, 'evaluation_ready': warning is None, 'evaluation_warning': warning}
