"""Shared evening archives, account-owned feedback and administrator jobs."""
from __future__ import annotations

import sqlite3
from datetime import date as _date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.services import evening_report_jobs, evening_report_tracking
from app.services.evening_report_store import EveningReportStore

router = APIRouter(prefix='/api/evening-report', tags=['evening-report'])


class RebuildRequest(BaseModel):
    date: _date
    mode: Literal['live', 'revision'] = 'live'
    diagnostics: bool = Field(default=False, strict=True)


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    date: _date
    version: str = Field(pattern=r'^[a-f0-9]{32}$')
    symbol: str = Field(pattern=r'^\d{6}\.(SH|SZ|BJ)$')
    starred: bool = Field(strict=True)
    outcome: Literal['profit', 'loss'] | None


@router.get('/personal')
def personal(request: Request, date: _date,
             version: str = Query(pattern=r'^[a-f0-9]{32}$')) -> dict:
    return evening_report_tracking.personal_report(request.app.state.repo.store.data_dir, date.isoformat(), version)


@router.put('/feedback')
def feedback(request: Request, body: FeedbackRequest) -> dict:
    try:
        return evening_report_tracking.update_feedback(
            request.app.state.repo.store.data_dir, day=body.date.isoformat(), version=body.version,
            symbol=body.symbol, starred=body.starred, outcome=body.outcome)
    except evening_report_tracking.FeedbackConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail='个人记录暂时无法保存，请刷新确认后重试') from exc


@router.get('/history')
def personal_history(request: Request, month: str | None = Query(default=None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$')) -> dict:
    return evening_report_tracking.personal_history(request.app.state.repo.store.data_dir, month)


@router.get('/overview')
def personal_overview(request: Request) -> dict:
    return evening_report_tracking.personal_overview(request.app.state.repo.store.data_dir)


@router.get('/dates')
def dates(request: Request, mode: Literal['live'] = 'live') -> dict:
    return {'dates': EveningReportStore(request.app.state.repo.store.data_dir).dates()}


@router.get('/status')
def status(request: Request, date: _date | None = None) -> dict:
    return evening_report_jobs.status(request.app.state.repo.store.data_dir, date.isoformat() if date else None)


@router.get('')
def report(request: Request, date: _date | None = None,
           version: str | None = Query(default=None, pattern=r'^[a-f0-9]{32}$'),
           mode: Literal['live'] = 'live') -> dict:
    store = EveningReportStore(request.app.state.repo.store.data_dir)
    if date is None:
        latest = evening_report_jobs.status(request.app.state.repo.store.data_dir)
        if latest['status'] != 'published':
            return latest
        date = _date.fromisoformat(latest['date'])
    result = store.get(date.isoformat(), version, view=True)
    if result is None:
        if store.original_metadata(date.isoformat(), version):
            return {'date': date.isoformat(), 'status': 'missing', 'reason': '该日晚报已定稿，归档文件暂不可读'}
        if version:
            raise HTTPException(status_code=404, detail='该日期的报告版本不存在')
        return evening_report_jobs.status(request.app.state.repo.store.data_dir, date.isoformat())
    return result


@router.post('/rebuild')
async def rebuild(request: Request, body: RebuildRequest) -> dict:
    return await evening_report_jobs.trigger(request.app.state.repo,
                                             getattr(request.app.state, 'quote_service', None),
                                             day=body.date, revision=body.mode == 'revision',
                                             diagnostics=body.diagnostics,
                                             capset=request.app.state.capabilities)
