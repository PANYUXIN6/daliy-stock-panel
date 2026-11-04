"""Independent evening generation using the collector's existing job and write slots."""
from __future__ import annotations

import logging
import threading
from contextvars import copy_context
from datetime import date, datetime
from pathlib import Path
from time import time_ns

from fastapi import HTTPException

from app.market_time import cn_now
from app.services.evening_report_inputs import load_inputs, require_ready
from app.services.evening_report_store import (
    EveningReportStore,
    RevisionBranch,
    atomic_json,
    history_view,
    read_json,
)

logger = logging.getLogger(__name__)
_ATTEMPT_LOCK = threading.RLock()


def _attempt_path(data_dir: Path, day: str) -> Path:
    return Path(data_dir) / 'evening_report' / 'attempts' / f'{date.fromisoformat(day)}.json'


def _read_attempt(data_dir: Path, day: str, mode: str | None = None) -> dict:
    attempts = read_json(_attempt_path(data_dir, day)) or {}
    if mode:
        return attempts.get(mode, {})
    return max(attempts.values(), key=lambda value: value.get('updated_ns', 0), default={})


def _save_attempt(data_dir: Path, day: str, mode: str, value: dict) -> None:
    with _ATTEMPT_LOCK:
        attempts = read_json(_attempt_path(data_dir, day)) or {}
        attempts[mode] = {**value, 'mode': mode, 'updated_ns': time_ns()}
        atomic_json(_attempt_path(data_dir, day), attempts)


def status(data_dir: Path, day: str | None = None, *, mode: str | None = None) -> dict:
    from app.services.pipeline_jobs import job_store

    day = day or cn_now().date().isoformat()
    store = EveningReportStore(data_dir)
    attempt = _read_attempt(data_dir, day, mode)
    job = job_store.get(attempt['job_id']) if attempt.get('job_id') else None
    if job and job['status'] in ('pending', 'running'):
        return {**attempt, 'date': day, 'status': 'generating', 'reason': '正在核验输入并计算每日晚报',
                'progress': job.get('progress', 0)}
    if attempt.get('status') in ('failed', 'cancelled'):
        return {**attempt, 'date': day, 'has_report': store.has_original(day)}
    if job and job['status'] == 'failed':
        # JobStore startup recovery also covers a process dying before our callback.
        return {**attempt, 'date': day, 'status': 'cancelled' if job.get('outcome') == 'cancelled' else 'failed',
                'reason': job.get('error')}
    if store.has_original(day):
        return {**attempt, 'date': day, 'status': 'published', 'reason': '原发布已归档'}
    ready = store.readiness_status(day)
    complete = bool(ready and ready.get('complete'))
    return {'date': day, 'status': 'ready' if complete else 'waiting',
            'reason': '输入已验收，等待执行槽' if complete else
                      (ready or {}).get('reason', '等待正式行情、分类与市场环境完成本轮验收')}


def _reference(evidence: dict) -> dict:
    return {'input_id': evidence['input_id'], 'versions': evidence['versions'], 'cutoff': evidence['cutoff'],
            'classifications': [o['id'] for o in evidence['classifications']]}


def _recover_evidence(data_dir: Path, day: date) -> dict:
    store = EveningReportStore(data_dir)
    current = store.readiness(day.isoformat())
    if current and current.get('complete'):
        return require_ready(data_dir, day)
    original = store.original_metadata(day.isoformat())
    input_id = (original or {}).get('input_id')
    if not input_id:
        from app.services.daily_coverage import DailyCoverageError
        raise DailyCoverageError(f'{day} 没有保留当时完整输入证据，不能恢复历史修订')
    return require_ready(data_dir, day, input_id=input_id)


def _revision_plan(data_dir: Path, start: date) -> tuple[list[tuple[date, str]], list[str]]:
    store = EveningReportStore(data_dir)
    original_dates = [entry['date'] for entry in store.dates() if store.has_original(entry['date'])]
    latest = max(original_dates)
    last = _recover_evidence(data_dir, date.fromisoformat(latest))
    calendar = [day for day in last['calendar']['days'] if start.isoformat() <= day <= latest]
    plan, gaps = [], []
    for value in calendar:
        day = date.fromisoformat(value)
        current = store.readiness(value)
        if value not in original_dates and not (current and current.get('complete')):
            # A missing original observation is a real gap, not a borrowed state.
            gaps.append(value)
            continue
        plan.append((day, _recover_evidence(data_dir, day)['input_id']))
    if not plan or plan[0][0] != start:
        from app.services.daily_coverage import DailyCoverageError
        raise DailyCoverageError(f'{start} 不在可恢复修订交易日范围内')
    return plan, gaps


def generate(repo, day: date, *, revision: bool, progress, automatic: bool = False, authorize=None) -> dict:
    from app.services.evening_report_engine import SCOPE_VERSION, build_report
    from app.services.evening_report_rules import RULE_VERSION, SELECTION_POLICY_VERSION
    from app.services.shared_runtime import publish_data_update

    def check():
        progress('evening_report', 20, '核验输入并计算每日晚报…', skip_log=True)
        if automatic and day != cn_now().date():
            raise ValueError('晚报任务已跨日期，需管理员明确补建并披露迟到时间')

    check()
    if authorize:
        authorize()
    data_dir = repo.store.data_dir
    store = EveningReportStore(data_dir)
    if revision:
        plan, gaps = _revision_plan(data_dir, day)
    else:
        original = store.original_metadata(day.isoformat())
        if original:
            publish_data_update(data_dir, ['evening_report'])
            return {'date': day.isoformat(), 'version': original['version'], 'reused': True}
        plan, gaps = [(day, require_ready(data_dir, day)['input_id'])], []
    input_ids = {target.isoformat(): input_id for target, input_id in plan}
    if revision:
        for manifest in store.branch_manifests():
            if manifest.get('input_ids') == input_ids and manifest.get('rule_version') == RULE_VERSION and manifest.get('selection_policy_version') == SELECTION_POLICY_VERSION and manifest.get('scope_version') == SCOPE_VERSION:
                publish_data_update(data_dir, ['evening_report'])
                return {'date': day.isoformat(), 'version': manifest['dates'][day.isoformat()]['version'],
                        'branch_id': manifest['branch_id'], 'reused': True}
        if all((report := store.original_metadata(target.isoformat())) and report.get('input_id') == input_ids[target.isoformat()]
               and report.get('rule_version') == RULE_VERSION and report.get('selection_policy_version') == SELECTION_POLICY_VERSION and report.get('scope_version') == SCOPE_VERSION
               for target, _ in plan):
            publish_data_update(data_dir, ['evening_report'])
            return {'date': day.isoformat(), 'version': store.original_metadata(day.isoformat())['version'], 'reused': True}
    # Only the prefix before the affected date comes from live history. Every
    # subsequent state belongs to this branch, including observed gaps.
    history = store.history(day.isoformat(), RULE_VERSION)
    branch = RevisionBranch(store) if revision else None
    if branch:
        branch.gaps = gaps
    first_report = None
    try:
        for target, input_id in plan:
            check()
            evidence = require_ready(data_dir, target, input_id=input_id)
            inputs = load_inputs(data_dir, target, evidence, check)
            inputs.provenance['scope_version'] = SCOPE_VERSION
            payload = build_report(inputs, history=history, cancel_check=check)
            check()
            require_ready(data_dir, target, input_id=evidence['input_id'])
            payload['rule_version'] = RULE_VERSION
            payload['selection_policy_version'] = SELECTION_POLICY_VERSION
            payload['scope_version'] = SCOPE_VERSION
            payload['late_generation'] = target != cn_now().date()
            payload['recovery_note'] = ('恢复计算、当时未发布'
                                        if payload['late_generation'] and not store.has_original(target.isoformat()) else None)
            if authorize:
                authorize()
            reference = _reference(evidence)
            report = (branch.append(target.isoformat(), payload, reference) if branch else
                      store.save(target.isoformat(), payload, reference))
            history.append(history_view(report))
            history = history[-7:]
            if first_report is None:
                first_report = {'version': report['version']}
        if branch:
            for target, input_id in plan:
                check()
                require_ready(data_dir, target, input_id=input_id)
            check()
            if authorize:
                authorize()
            branch.commit()
        publish_data_update(data_dir, ['evening_report'])
        return {'date': day.isoformat(), 'version': first_report['version'],
                'branch_id': branch.id if branch else None, 'reused': False}
    finally:
        if branch:
            branch.close()


async def trigger(repo, quote_service=None, *, day: date, revision: bool = False, automatic: bool = False) -> dict:
    from app.api.pipeline import _submit_job
    from app.services.daily_coverage import DailyCoverageError
    from app.services.evening_report_engine import SCOPE_VERSION
    from app.services.evening_report_rules import RULE_VERSION, SELECTION_POLICY_VERSION

    data_dir = repo.store.data_dir
    store = EveningReportStore(data_dir)
    if revision and not store.has_original(day.isoformat()):
        raise HTTPException(status_code=409, detail='尚无原发布，不能创建修订版')
    try:
        original = store.original_metadata(day.isoformat())
        evidence = (store.input_evidence(original['input_id']) if original and not revision else
                    _recover_evidence(data_dir, day) if revision else require_ready(data_dir, day))
    except DailyCoverageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    authorize = None
    if not automatic:
        from app.services.auth import AuthError, require_current_session

        def authorize():
            if require_current_session()['role'] != 'admin':
                raise AuthError('共享晚报重建仅管理员可执行', 403, 'ADMIN_REQUIRED')
        authorize()
    context = copy_context()
    mode = 'revision' if revision else 'live'
    # The persisted observation IDs plus ready cutoff identify one accepted input;
    # no digest or duplicate market-data snapshot is introduced.
    key = f'evening_report:{day}:{mode}:{RULE_VERSION}:{SELECTION_POLICY_VERSION}:{SCOPE_VERSION}:{evidence["input_id"]}'
    result = await _submit_job(
        repo, quote_service,
        lambda progress: lambda: context.run(generate, repo, day, revision=revision, progress=progress,
                                             automatic=automatic, authorize=authorize),
        request_key=key, datasets=['evening_report'], finalize=lambda: None,
    )
    prior_attempt = _read_attempt(data_dir, day.isoformat(), mode)
    _save_attempt(data_dir, day.isoformat(), mode, {
        'date': day.isoformat(), 'status': 'generating', 'job_id': result['job_id'],
        'request_key': key, 'last_attempt_at': cn_now().isoformat(), 'reason': '等待执行槽',
        'manual_resume_at': prior_attempt.get('manual_resume_at') if automatic else cn_now().isoformat(),
    })
    return {**result, 'status': 'generating'}


async def after_job(repo, job: dict) -> None:
    """Runs only after the original task has released its write/capacity slots."""
    key = job.get('request_key') or ''
    data_dir = repo.store.data_dir
    if key.startswith('evening_report:'):
        day = key.split(':')[1]
        mode = key.split(':')[2]
        prior_attempt = _read_attempt(data_dir, day, mode)
        _save_attempt(data_dir, day, mode, {
            'date': day, 'job_id': job['id'], 'request_key': key,
            'status': 'published' if job['status'] == 'succeeded' else
                      'cancelled' if job.get('outcome') == 'cancelled' else 'failed',
            'last_attempt_at': job.get('finished_at'), 'reason': job.get('error'),
            'manual_resume_at': prior_attempt.get('manual_resume_at'),
        })
    elif key.startswith('postmarket:'):
        day = key.split(':')[1]
        if job.get('outcome') == 'cancelled':
            prior_attempt = _read_attempt(data_dir, day, 'live')
            if _later(prior_attempt.get('manual_resume_at'), job.get('finished_at')):
                return
            _save_attempt(data_dir, day, 'live', {
                'date': day, 'status': 'cancelled', 'reason': '盘后管道已手动取消，需管理员明确恢复晚报',
                'last_attempt_at': job.get('finished_at'),
            })
        else:
            await run_scheduled(repo)


def _later(first: str | None, second: str | None) -> bool:
    if not first or not second:
        return False
    return datetime.fromisoformat(first.replace('Z', '+00:00')) > datetime.fromisoformat(second.replace('Z', '+00:00'))


async def run_scheduled(repo) -> dict | None:
    from app.jobs.daily_pipeline import _get_app_state
    from app.services import preferences, trading_day

    now = cn_now()
    schedule = preferences.get_pipeline_schedule()
    if (now.hour, now.minute) < (schedule['hour'], schedule['minute']) or now.hour < 16:
        return None
    if trading_day.is_trading_day(now, data_dir=repo.store.data_dir) is not True:
        return None
    day = now.date()
    from app.services.pipeline_jobs import job_store

    prior_attempt = _read_attempt(repo.store.data_dir, day.isoformat(), 'live')
    latest_pipeline = next((job for job in job_store.list_recent(limit=200)
                            if job.get('request_key') == f'postmarket:{day}'), None)
    if (latest_pipeline and latest_pipeline.get('outcome') == 'cancelled'
            and not _later(prior_attempt.get('manual_resume_at'), latest_pipeline.get('finished_at'))):
        return None
    current = status(repo.store.data_dir, day.isoformat(), mode='live')
    if current['status'] in ('published', 'cancelled', 'generating', 'waiting'):
        return None
    app_state = _get_app_state()
    if app_state is None:
        return None
    try:
        return await trigger(repo, getattr(app_state, 'quote_service', None), day=day, automatic=True)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        logger.info('每日晚报等待：%s', exc.detail)
        return None
