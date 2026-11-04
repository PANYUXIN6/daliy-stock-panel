"""Read-only evening archives and administrator-triggered collector jobs."""
from __future__ import annotations

from datetime import date as _date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.services import evening_report_jobs
from app.services.evening_report_store import EveningReportStore

router = APIRouter(prefix='/api/evening-report', tags=['evening-report'])


class RebuildRequest(BaseModel):
    date: _date
    mode: Literal['live', 'revision'] = 'live'


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
        entries = store.dates()
        if not entries:
            return evening_report_jobs.status(request.app.state.repo.store.data_dir)
        date = _date.fromisoformat(entries[0]['date'])
    result = store.get(date.isoformat(), version, view=True)
    if result is None:
        if version:
            raise HTTPException(status_code=404, detail='该日期的报告版本不存在')
        return evening_report_jobs.status(request.app.state.repo.store.data_dir, date.isoformat())
    return result


@router.post('/rebuild')
async def rebuild(request: Request, body: RebuildRequest) -> dict:
    return await evening_report_jobs.trigger(request.app.state.repo,
                                             getattr(request.app.state, 'quote_service', None),
                                             day=body.date, revision=body.mode == 'revision')
