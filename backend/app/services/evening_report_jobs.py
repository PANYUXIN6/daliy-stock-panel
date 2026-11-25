"""Independent evening generation using the collector's existing job and write slots."""
from __future__ import annotations

import logging
import threading
from contextvars import copy_context
from datetime import date, datetime
from pathlib import Path

from fastapi import HTTPException

from app.market_time import CN_TZ, cn_now
from app.services.daily_coverage import DailyCoverageError
from app.services.evening_report_inputs import (
    load_inputs,
    prepare_wave_history,
    require_ready,
)
from app.services.evening_report_store import (
    EveningReportStore,
    atomic_json,
    read_json,
)

logger = logging.getLogger(__name__)
_ATTEMPT_LOCK = threading.RLock()


def _attempt_path(data_dir: Path, day: str) -> Path:
    return Path(data_dir) / 'evening_report' / 'attempts' / f'{date.fromisoformat(day)}.json'


def _read_attempt(data_dir: Path, day: str) -> dict:
    return read_json(_attempt_path(data_dir, day)) or {}


def _save_attempt(data_dir: Path, day: str, value: dict) -> None:
    with _ATTEMPT_LOCK:
        notified = _read_attempt(data_dir, day).get('notified_version')
        if notified:
            value = {'notified_version': notified, **value}
        atomic_json(_attempt_path(data_dir, day), value)


def _notify_final(data_dir: Path, day: str, version: str) -> None:
    from app.services.shared_runtime import publish_data_update

    with _ATTEMPT_LOCK:
        attempt = _read_attempt(data_dir, day)
        if attempt.get('notified_version') == version:
            return
        publish_data_update(data_dir, ['evening_report'])
        _save_attempt(data_dir, day, {**attempt, 'notified_version': version})


def status(data_dir: Path, day: str | None = None) -> dict:
    from app.services.pipeline_jobs import job_store
    from app.services.trading_day import calendar_status

    store = EveningReportStore(data_dir)
    if not day:
        today = cn_now().date().isoformat()
        day = max((value for value in calendar_status(data_dir).get('days', []) if value <= today), default='')
        if not day:
            day = max((entry['date'] for entry in store.dates() if entry['date'] <= today), default='')
        if not day:
            return {'date': '', 'status': 'waiting', 'reason': '交易日历尚未就绪，暂时无法确定最近交易日'}
    original = store.original_metadata(day)
    if original:
        return {'date': day, 'status': 'published', 'version': original['version'], 'has_report': True}
    now = cn_now()
    if day != now.date().isoformat():
        return {'date': day, 'status': 'unpublished', 'reason': '该日无正式晚报'}
    blocker = _generation_blocker(now.date(), now, data_dir)
    can_generate = blocker is None
    attempt = {**_read_attempt(data_dir, day), 'can_generate': can_generate}
    job = job_store.get(attempt['job_id']) if attempt.get('job_id') else None
    if job and job['status'] in ('pending', 'running'):
        message = next((entry['msg'] for entry in reversed(job.get('log', [])) if not entry.get('task_id')), None)
        return {**attempt, 'date': day, 'status': 'generating', 'reason': message or '正在核验输入并计算每日晚报',
                'progress': job.get('progress', 0)}
    if attempt.get('status') in ('failed', 'cancelled'):
        return {**attempt, 'date': day, 'has_report': store.has_original(day)}
    if job and job['status'] == 'failed':
        # JobStore startup recovery also covers a process dying before our callback.
        return {**attempt, 'date': day, 'status': 'cancelled' if job.get('outcome') == 'cancelled' else 'failed',
                'reason': job.get('error')}
    ready = store.readiness_status(day)
    complete = bool(ready and ready.get('complete'))
    return {'date': day, 'can_generate': can_generate, 'status': 'ready' if complete else 'waiting',
            'reason': blocker or ('输入已验收，等待执行槽' if complete else
                      (ready or {}).get('reason', '等待正式行情、分类与市场环境完成本轮验收'))}


def _reference(evidence: dict) -> dict:
    return {'input_id': evidence['input_id'], 'versions': evidence['versions'], 'cutoff': evidence['cutoff'],
            'classifications': [o['id'] for o in evidence['classifications']]}


def generate(repo, day: date, *, progress, authorize=None, diagnostics: bool = False) -> dict:
    from app.services.evening_report_engine import build_report
    from app.services.evening_report_rules import RULE_VERSION
    from app.services.evening_report_start_study import archive_study_market

    data_dir = repo.store.data_dir
    store = EveningReportStore(data_dir)
    report_date = day.isoformat()
    if authorize:
        authorize()
    original = store.original_metadata(report_date)
    if original:
        _notify_final(data_dir, report_date, original['version'])
        return {'date': report_date, 'status': 'published', 'version': original['version'], 'reused': True}

    def check():
        progress('evening_report', 20, '核验输入并计算每日晚报…', skip_log=True)
        _require_preparation_time(day)

    check()
    evidence = require_ready(data_dir, day)
    history = store.history(report_date, RULE_VERSION, allow_partial=True)
    inputs = load_inputs(data_dir, day, evidence, check)
    lookups = store.published_wave_lookups(report_date)
    if inputs.wave_factors is not None:
        checkpoint = history[-1].get('state', {}).get('wave', {}) if history else {}
        prepare_wave_history(store, inputs, evidence, checkpoint)
    published_start, published_close, published_structure_event = lookups
    payload = build_report(inputs, history=history, cancel_check=check,
                           published_start=published_start, published_close=published_close,
                           published_structure_event=published_structure_event)
    archive_study_market(store, inputs, payload, history)
    require_ready(data_dir, day, input_id=evidence['input_id'])
    if diagnostics:
        store.save_diagnostic(payload, reason='administrator_requested')
    def before_publish():
        check()
        if authorize:
            authorize()

    check()
    report = store.save(report_date, payload, _reference(evidence), before_publish=before_publish)
    _notify_final(data_dir, report_date, report['version'])
    return {'date': report_date, 'status': 'published', 'version': report['version'], 'reused': False}


def _generation_blocker(day: date, now: datetime, data_dir: Path | None = None) -> str | None:
    from app.services.trading_day import is_trading_day

    if day != now.date():
        return '晚报任务已跨日期，该日保留缺报状态'
    if now.hour < 16:
        return '晚报数据准备须在北京时间16:00后执行，等待正式收盘数据'
    if data_dir is not None and is_trading_day(now, data_dir=data_dir) is False:
        return '交易日历已确认当日休市'
    return None


def _require_preparation_time(day: date) -> None:
    if blocker := _generation_blocker(day, cn_now()):
        raise DailyCoverageError(blocker)


def _prepare_and_generate(repo, quote_service, capset, day: date, *,
                          progress, automatic: bool, authorize, diagnostics: bool) -> dict:
    from app.api.pipeline import finalize_market_data
    from app.jobs import daily_pipeline
    from app.services import trading_day

    data_dir = repo.store.data_dir
    if authorize:
        authorize()
    if EveningReportStore(data_dir).has_original(day.isoformat()):
        return generate(repo, day, progress=progress, authorize=authorize)
    _require_preparation_time(day)
    progress('evening_report', 0, '核验每日晚报输入…')
    try:
        require_ready(data_dir, day)
    except DailyCoverageError:
        if automatic:
            raise
        if capset is None:
            raise DailyCoverageError('数据采集服务能力尚未就绪，请稍后重试') from None
        progress('prepare_evening_report', 0, '准备每日晚报所需数据…')
        if trading_day.is_trading_day(cn_now(), data_dir=data_dir) is None:
            progress('sync_calendar', 0, '更新交易日历并核对报告日期…')
            trading_day.sync_calendar(data_dir)
        progress('prepare_evening_report', 0, '核对交易日期，准备正式行情…')
        _require_preparation_time(day)
        if trading_day.is_trading_day(cn_now(), data_dir=data_dir) is not True:
            raise DailyCoverageError('目标日期不是已确认的交易日，不能准备晚报输入') from None
        if authorize:
            authorize()

        def preparation_progress(stage, pct, message, **kwargs):
            progress(stage, int(pct * .7), message, **kwargs)

        try:
            daily_pipeline.run_now(repo, capset, on_progress=preparation_progress,
                                   postmarket=True, target_date=day, evening_only=True)
        finally:
            # Preparation may have written data even when acceptance or cancellation fails.
            finalize_market_data(repo, quote_service)
        progress('evening_report', 75, '必要数据验收完成，开始生成每日晚报…')
        _require_preparation_time(day)
        require_ready(data_dir, day)
    return generate(repo, day, progress=progress, authorize=authorize, diagnostics=diagnostics)


async def trigger(repo, quote_service=None, *, day: date, revision: bool = False, automatic: bool = False,
                  diagnostics: bool = False, capset=None) -> dict:
    from app.api.pipeline import _submit_job
    from app.services.evening_report_rules import RULE_VERSION, SELECTION_POLICY_VERSION

    data_dir = repo.store.data_dir
    store = EveningReportStore(data_dir)
    authorize = None
    if not automatic:
        from app.services.auth import AuthError, require_current_session

        def authorize():
            if require_current_session()['role'] != 'admin':
                raise AuthError('共享晚报重建仅管理员可执行', 403, 'ADMIN_REQUIRED')
        authorize()
    if revision:
        raise HTTPException(status_code=409, detail='每日晚报发布即定稿，修订写入口已关闭')
    report_date = day.isoformat()
    original = store.original_metadata(report_date)
    if original:
        try:
            _notify_final(data_dir, report_date, original['version'])
        except OSError as exc:
            raise HTTPException(status_code=503, detail='晚报已定稿，刷新通知仍待确认，可重试') from exc
        return {'date': report_date, 'status': 'published', 'version': original['version'], 'reused': True}
    now = cn_now()
    if day != now.date():
        return {'date': report_date, 'status': 'unpublished', 'reason': '该日无正式晚报'}
    if blocker := _generation_blocker(day, now, data_dir):
        raise HTTPException(status_code=409, detail=blocker)
    try:
        evidence = require_ready(data_dir, day)
    except DailyCoverageError as exc:
        if automatic:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        evidence = None
    context = copy_context()
    # The persisted observation IDs plus ready cutoff identify one accepted input;
    # no digest or duplicate market-data snapshot is introduced.
    input_key = f'{evidence["scope_version"]}:{evidence["input_id"]}' if evidence else 'prepare'
    key = f'evening_report:{day}:live:{RULE_VERSION}:{SELECTION_POLICY_VERSION}:{input_key}'
    result = await _submit_job(
        repo, quote_service,
        lambda progress: lambda: context.run(_prepare_and_generate, repo, quote_service, capset, day,
                                             progress=progress,
                                             automatic=automatic, authorize=authorize, diagnostics=diagnostics),
        request_key=key, datasets=['evening_report'], finalize=lambda: None,
    )
    prior_attempt = _read_attempt(data_dir, report_date)
    _save_attempt(data_dir, report_date, {
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
        prior_attempt = _read_attempt(data_dir, day)
        _save_attempt(data_dir, day, {
            'date': day, 'job_id': job['id'], 'request_key': key,
            'status': 'published' if job['status'] == 'succeeded' else
                      'cancelled' if job.get('outcome') == 'cancelled' else 'failed',
            'last_attempt_at': job.get('finished_at'), 'reason': job.get('error'),
            'manual_resume_at': prior_attempt.get('manual_resume_at'),
        })
        if job['status'] == 'succeeded':
            # Derived history is independent from shared report publication.
            # It reads only accepted local inputs and is retried on later runs.
            await _queue_tracking(repo, day)
    elif key.startswith('postmarket:'):
        day = key.split(':')[1]
        if job.get('outcome') == 'cancelled':
            prior_attempt = _read_attempt(data_dir, day)
            if _later(prior_attempt.get('manual_resume_at'), job.get('finished_at')):
                return
            _save_attempt(data_dir, day, {
                'date': day, 'status': 'cancelled', 'reason': '盘后管道已手动取消，需管理员明确恢复晚报',
                'last_attempt_at': job.get('finished_at'),
            })
        else:
            await run_scheduled(repo)
            if EveningReportStore(data_dir).has_original(day):
                await _queue_tracking(repo, day)


async def _queue_tracking(repo, day: str) -> dict | None:
    from app.api.pipeline import _submit_job
    from app.services.evening_report_tracking import refresh_after_publication, refresh_completed
    from app.services.pipeline_jobs import job_store

    data_dir = repo.store.data_dir
    store = EveningReportStore(data_dir)
    entries = store.dates()
    if not entries:
        return None
    day = max(day, entries[0]['date'])
    evidence = store.readiness(day) or {}
    newest = max((entry['versions'][0] for entry in entries),
                 key=lambda value: (value.get('generated_at', ''), value['version']))
    key = f'evening_tracking:{day}:v1:{newest["version"]}:{evidence.get("input_id", "unready")}'
    if refresh_completed(data_dir, key):
        return None
    # All automatic derivation paths pass here. Use persisted terminal outcomes
    # so cancellation while queued and cancellation before a restart both count.
    now = cn_now()
    resumes = [_read_attempt(data_dir, day).get('manual_resume_at')]
    for job in job_store.list_recent(limit=200):
        if not (job.get('request_key') or '').startswith('evening_tracking:') or job.get('outcome') != 'cancelled':
            continue
        cancelled_at = datetime.fromisoformat(job['finished_at'].replace('Z', '+00:00')).astimezone(CN_TZ)
        if cancelled_at.date() == now.date() and not any(_later(resume, job['finished_at']) for resume in resumes):
            return None

    def make_run(progress):
        def check():
            progress('evening_tracking', 30, '记录观察历史并更新到期价格表现…', skip_log=True)

        return lambda: refresh_after_publication(data_dir, day, cancel_check=check, request_key=key)

    try:
        return await _submit_job(repo, None, make_run, request_key=key,
                                 datasets=['evening_report_tracking'], finalize=lambda: None,
                                 pause_quotes=False)
    except HTTPException:
        logger.exception('晚报已保留，观察历史派生任务暂未取得执行槽，后续盘后任务将重试')
        return None


def _later(first: str | None, second: str | None) -> bool:
    if not first or not second:
        return False
    return datetime.fromisoformat(first.replace('Z', '+00:00')) > datetime.fromisoformat(second.replace('Z', '+00:00'))


async def run_retention(repo) -> dict | None:
    """Daily bounded maintenance, serialized with collection and publication."""
    from app.api.pipeline import _submit_job
    from app.services.evening_report_tracking import prune_tracking
    from app.services.pipeline_jobs import job_store

    data_dir = repo.store.data_dir
    store = EveningReportStore(data_dir)
    if not store.root.exists():
        return None
    today = cn_now().date()
    key = f'evening_retention:{today}'
    if any(job.get('request_key') == key and
           (job['status'] == 'succeeded' or job.get('outcome') == 'cancelled')
           for job in job_store.list_recent(limit=200)):
        return None

    def make_run(progress):
        def check():
            progress('evening_retention', 50, '清理到期晚报审计与临时证据…', skip_log=True)

        def run():
            result = store.prune(today, check)
            return {**result, **prune_tracking(data_dir, today, check)}

        return run

    try:
        return await _submit_job(repo, None, make_run, request_key=key, datasets=[],
                                 finalize=lambda: None, pause_quotes=False)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        logger.info('晚报存储维护等待数据任务完成')
        return None


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

    prior_attempt = _read_attempt(repo.store.data_dir, day.isoformat())
    latest_pipeline = next((job for job in job_store.list_recent(limit=200)
                            if job.get('request_key') == f'postmarket:{day}'), None)
    if (latest_pipeline and latest_pipeline.get('outcome') == 'cancelled'
            and not _later(prior_attempt.get('manual_resume_at'), latest_pipeline.get('finished_at'))):
        return None
    current = status(repo.store.data_dir, day.isoformat())
    if current['status'] == 'published':
        try:
            _notify_final(repo.store.data_dir, day.isoformat(), current['version'])
        except OSError:
            logger.warning('晚报已定稿，刷新通知待重试：%s', day, exc_info=True)
        return await _queue_tracking(repo, day.isoformat())
    if current['status'] in ('cancelled', 'generating', 'waiting'):
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
