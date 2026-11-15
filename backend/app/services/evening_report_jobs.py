"""Independent evening generation using the collector's existing job and write slots."""
from __future__ import annotations

import logging
import threading
from contextvars import copy_context
from datetime import date, datetime
from pathlib import Path
from time import time_ns

from fastapi import HTTPException

from app.market_time import CN_TZ, cn_now
from app.services.daily_coverage import DailyCoverageError
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
    from app.services.trading_day import calendar_status

    store = EveningReportStore(data_dir)
    if not day:
        today = cn_now().date().isoformat()
        day = max((value for value in calendar_status(data_dir).get('days', []) if value <= today), default='')
        if not day:
            day = max((entry['date'] for entry in store.dates() if entry['date'] <= today), default='')
        if not day:
            return {'date': '', 'status': 'waiting', 'reason': '交易日历尚未就绪，暂时无法确定最近交易日'}
    attempt = _read_attempt(data_dir, day, mode)
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
    if store.has_original(day):
        return {**attempt, 'date': day, 'status': 'published', 'reason': '原发布已归档'}
    ready = store.readiness_status(day)
    complete = bool(ready and ready.get('complete'))
    if not ready and day < cn_now().date().isoformat():
        return {'date': day, 'status': 'waiting', 'reason': '尚无该日完整输入；管理员可重试生成，核验保留的当日数据后恢复'}
    return {'date': day, 'status': 'ready' if complete else 'waiting',
            'reason': '输入已验收，等待执行槽' if complete else
                      (ready or {}).get('reason', '等待正式行情、分类与市场环境完成本轮验收')}


def _reference(evidence: dict) -> dict:
    return {'input_id': evidence['input_id'], 'versions': evidence['versions'], 'cutoff': evidence['cutoff'],
            'classifications': [o['id'] for o in evidence['classifications']]}


def _recover_evidence(data_dir: Path, day: date) -> dict:
    store = EveningReportStore(data_dir)
    current = store.readiness(day.isoformat())
    # Today's acceptance may have been revoked by a new pipeline attempt.
    # Archived evidence can restore historical days, but cannot undo that revocation.
    if day == cn_now().date() or (current and current.get('complete')):
        return require_ready(data_dir, day)
    original = store.original_metadata(day.isoformat())
    input_id = (original or {}).get('input_id')
    if not input_id:
        raise DailyCoverageError(f'{day} 没有保留当时完整输入证据，不能恢复历史修订')
    return require_ready(data_dir, day, input_id=input_id)


def _revision_plan(data_dir: Path, start: date, *, defer_today: bool = False) -> tuple[list[tuple[date, str | None]], list[str]]:
    from app.services.evening_report_rules import RULE_VERSION

    store = EveningReportStore(data_dir)
    store.history(start.isoformat(), RULE_VERSION)
    original_dates = [entry['date'] for entry in store.dates() if store.has_original(entry['date'])]
    latest = max(original_dates)
    today = cn_now().date()
    # Use the saved calendar only to enumerate dependencies, then accept every
    # input in chronological order. A revoked latest input must not hide an
    # unrecoverable earlier day behind today's preparable failure.
    last = store.readiness(latest)
    if not last or not last.get('complete'):
        input_id = store.original_metadata(latest).get('input_id')
        last = store.input_evidence(input_id) if input_id else None
    if (not last or not last.get('calendar')) and defer_today and latest == today.isoformat():
        from app.services.trading_day import calendar_status

        # Today's missing snapshot can be rebuilt. This provisional calendar
        # checks known historical dependencies; generation rebuilds the plan
        # from the newly accepted input before any branch can be published.
        days = sorted(set(calendar_status(data_dir).get('days', [])) | set(original_dates))
    else:
        if not last or not last.get('calendar'):
            last = _recover_evidence(data_dir, date.fromisoformat(latest))
        days = last['calendar']['days']
    calendar = sorted({day for day in days if start.isoformat() <= day <= latest})
    plan, gaps = [], []
    for value in calendar:
        day = date.fromisoformat(value)
        current = store.readiness(value)
        if value not in original_dates and not (current and current.get('complete')):
            # A missing original observation is a real gap, not a borrowed state.
            gaps.append(value)
            continue
        try:
            input_id = _recover_evidence(data_dir, day)['input_id']
        except DailyCoverageError:
            if not defer_today or day != today:
                raise
            input_id = None
        plan.append((day, input_id))
    if not plan or plan[0][0] != start or plan[-1][0].isoformat() != latest:
        raise DailyCoverageError(f'{start} 不在可恢复修订交易日范围内')
    return plan, gaps


def generate(repo, day: date, *, revision: bool, progress, automatic: bool = False, authorize=None,
             diagnostics: bool = False) -> dict:
    from app.services.evening_report_engine import build_report
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
        scope_versions = {target.isoformat(): require_ready(data_dir, target, input_id=input_id)['scope_version']
                          for target, input_id in plan}
        for manifest in store.branch_manifests():
            if (manifest.get('input_ids') == input_ids and manifest.get('rule_version') == RULE_VERSION
                    and manifest.get('selection_policy_version') == SELECTION_POLICY_VERSION
                    and all(manifest['dates'][value].get('scope_version') == scope
                            for value, scope in scope_versions.items())):
                publish_data_update(data_dir, ['evening_report'])
                return {'date': day.isoformat(), 'version': manifest['dates'][day.isoformat()]['version'],
                        'branch_id': manifest['branch_id'], 'reused': True}
        if all((report := store.original_metadata(target.isoformat())) and report.get('input_id') == input_ids[target.isoformat()]
               and report.get('rule_version') == RULE_VERSION and report.get('selection_policy_version') == SELECTION_POLICY_VERSION
               and report.get('scope_version') == scope_versions[target.isoformat()]
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
            payload = build_report(inputs, history=history, cancel_check=check)
            check()
            require_ready(data_dir, target, input_id=evidence['input_id'])
            payload['rule_version'] = RULE_VERSION
            payload['selection_policy_version'] = SELECTION_POLICY_VERSION
            payload['late_generation'] = target != cn_now().date()
            payload['recovery_note'] = ('恢复计算、当时未发布'
                                        if payload['late_generation'] and not store.has_original(target.isoformat()) else None)
            if authorize:
                authorize()
            reference = _reference(evidence)
            if diagnostics:
                store.save_diagnostic(payload, reason='administrator_requested')
            report = (branch.append(target.isoformat(), payload, reference) if branch else
                      store.save(target.isoformat(), payload, reference))
            history.append(history_view({**payload, 'input_reference': reference}))
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


def _generation_evidence(data_dir: Path, day: date, revision: bool, *, allow_recovery: bool = False) -> dict | None:
    """Manual missing originals may reaccept dated facts; revisions stay exact."""
    original = EveningReportStore(data_dir).original_metadata(day.isoformat())
    if original and not revision:
        return {'input_id': original['input_id'], 'scope_version': original.get('scope_version')}
    if revision:
        plan, _ = _revision_plan(data_dir, day, defer_today=True)
        if any(input_id is None for _, input_id in plan):
            return None
        return require_ready(data_dir, day, input_id=plan[0][1])
    try:
        return require_ready(data_dir, day)
    except DailyCoverageError:
        if day != cn_now().date() and not (allow_recovery and day < cn_now().date()):
            raise
        return None


def _require_preparation_time(day: date) -> None:
    now = cn_now()
    if day != now.date():
        raise DailyCoverageError(f'{day} 缺少可恢复的当时输入；只能准备今日数据，不能用当前分类补造历史晚报')
    if now.hour < 16:
        raise DailyCoverageError('晚报数据准备须在北京时间16:00后执行，等待正式收盘数据')


def _prepare_and_generate(repo, quote_service, capset, day: date, *, revision: bool,
                          progress, automatic: bool, authorize, diagnostics: bool,
                          current_day: date | None, requested_on: date) -> dict:
    from app.api.pipeline import finalize_market_data
    from app.jobs import daily_pipeline
    from app.services import trading_day

    data_dir = repo.store.data_dir
    # A publication ahead of this queued revision can extend its dependency range.
    if revision and EveningReportStore(data_dir).has_original(requested_on.isoformat()):
        current_day = requested_on

    def emit(stage, pct, message, **kwargs):
        progress(stage, pct, message, **kwargs)
        if current_day is not None and current_day != cn_now().date():
            raise DailyCoverageError('晚报任务已跨日期，请重新提交以明确恢复历史输入')

    emit('evening_report', 0, '核验每日晚报输入…')
    if authorize:
        authorize()
    if _generation_evidence(data_dir, day, revision, allow_recovery=not automatic) is None:
        if automatic:
            raise DailyCoverageError('等待正式行情、分类与市场环境完成本轮验收')
        if day < cn_now().date() and not revision:
            from app.services.evening_report_recovery import recover_ready

            emit('prepare_evening_report', 10, '核验保留的历史行情、分类与停牌依据…')
            recover_ready(repo, day, check_cancel=lambda: emit(
                'prepare_evening_report', 30, '正在重新验收历史输入…', skip_log=True), authorize=authorize)
            return generate(repo, day, revision=False,
                            progress=lambda stage, pct, msg, **kwargs: emit(stage, 75 + int(pct * .24), msg, **kwargs),
                            automatic=False, authorize=authorize, diagnostics=diagnostics)
        preparation_day = current_day or cn_now().date()
        _require_preparation_time(preparation_day)
        if capset is None:
            raise DailyCoverageError('数据采集服务能力尚未就绪，请稍后重试') from None
        emit('prepare_evening_report', 0, '准备每日晚报所需数据…')
        if trading_day.is_trading_day(cn_now(), data_dir=data_dir) is None:
            emit('sync_calendar', 0, '更新交易日历并核对报告日期…')
            trading_day.sync_calendar(data_dir)
        emit('prepare_evening_report', 0, '核对交易日期，准备正式行情…')
        _require_preparation_time(preparation_day)
        if trading_day.is_trading_day(cn_now(), data_dir=data_dir) is not True:
            raise DailyCoverageError('目标日期不是已确认的交易日，不能准备晚报输入') from None
        if authorize:
            authorize()

        def preparation_progress(stage, pct, message, **kwargs):
            emit(stage, int(pct * .7), message, **kwargs)

        try:
            daily_pipeline.run_now(repo, capset, on_progress=preparation_progress,
                                   postmarket=True, target_date=preparation_day, evening_only=True)
        finally:
            # Preparation may have written data even when acceptance or cancellation fails.
            finalize_market_data(repo, quote_service)
        emit('evening_report', 75, '必要数据验收完成，开始生成每日晚报…')
        _require_preparation_time(preparation_day)
        require_ready(data_dir, preparation_day)
    return generate(repo, day, revision=revision,
                    progress=lambda stage, pct, msg, **kwargs: emit(stage, 75 + int(pct * .24), msg, **kwargs),
                    automatic=automatic, authorize=authorize, diagnostics=diagnostics)


async def trigger(repo, quote_service=None, *, day: date, revision: bool = False, automatic: bool = False,
                  diagnostics: bool = False, capset=None) -> dict:
    from app.api.pipeline import _submit_job
    from app.services.evening_report_rules import RULE_VERSION, SELECTION_POLICY_VERSION

    data_dir = repo.store.data_dir
    store = EveningReportStore(data_dir)
    if revision and not store.has_original(day.isoformat()):
        raise HTTPException(status_code=409, detail='尚无原发布，不能创建修订版')
    requested_on = cn_now().date()
    current_day = requested_on if (
        (day == requested_on and (revision or not store.has_original(day.isoformat())))
        or (revision and day < requested_on and store.has_original(requested_on.isoformat()))
    ) else None
    authorize = None
    if not automatic:
        from app.services.auth import AuthError, require_current_session

        def authorize():
            if require_current_session()['role'] != 'admin':
                raise AuthError('共享晚报重建仅管理员可执行', 403, 'ADMIN_REQUIRED')
        authorize()
    try:
        evidence = _generation_evidence(data_dir, day, revision, allow_recovery=not automatic)
        if evidence is None:
            if automatic:
                raise DailyCoverageError('等待正式行情、分类与市场环境完成本轮验收')
            if day >= requested_on or revision:
                _require_preparation_time(requested_on)
    except DailyCoverageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    context = copy_context()
    mode = 'revision' if revision else 'live'
    # The persisted observation IDs plus ready cutoff identify one accepted input;
    # no digest or duplicate market-data snapshot is introduced.
    input_key = f'{evidence["scope_version"]}:{evidence["input_id"]}' if evidence else 'prepare'
    key = f'evening_report:{day}:{mode}:{RULE_VERSION}:{SELECTION_POLICY_VERSION}:{input_key}'
    result = await _submit_job(
        repo, quote_service,
        lambda progress: lambda: context.run(_prepare_and_generate, repo, quote_service, capset, day,
                                             revision=revision, progress=progress,
                                             automatic=automatic, authorize=authorize, diagnostics=diagnostics,
                                             current_day=current_day, requested_on=requested_on),
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
        if job['status'] == 'succeeded':
            # Derived history is independent from shared report publication.
            # It reads only accepted local inputs and is retried on later runs.
            await _queue_tracking(repo, day)
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
    newest = max((version for entry in entries for version in entry['versions']),
                 key=lambda value: (value.get('generated_at', ''), value['version']))
    key = f'evening_tracking:{day}:v1:{newest["version"]}:{evidence.get("input_id", "unready")}'
    if refresh_completed(data_dir, key):
        return None
    # All automatic derivation paths pass here. Use persisted terminal outcomes
    # so cancellation while queued and cancellation before a restart both count.
    now = cn_now()
    resumes = [_read_attempt(data_dir, day, mode).get('manual_resume_at') for mode in ('live', 'revision')]
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

    prior_attempt = _read_attempt(repo.store.data_dir, day.isoformat(), 'live')
    latest_pipeline = next((job for job in job_store.list_recent(limit=200)
                            if job.get('request_key') == f'postmarket:{day}'), None)
    if (latest_pipeline and latest_pipeline.get('outcome') == 'cancelled'
            and not _later(prior_attempt.get('manual_resume_at'), latest_pipeline.get('finished_at'))):
        return None
    current = status(repo.store.data_dir, day.isoformat(), mode='live')
    if current['status'] == 'published':
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
