"""Authenticated local sector review queries; no collector or provider calls."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.services import sector_review
from app.services.sector_review_rules import RULES_VERSION
from app.tickflow.sector_review_reader import ReviewDataUnavailableError

router = APIRouter(prefix="/api/sector-review", tags=["sector-review"])


class ReviewQuery(BaseModel):
    kind: Literal["industry", "concept"] = "industry"
    dimension_id: str | None = Field(None, max_length=64)
    level: int = Field(1, ge=1, le=3)
    benchmark: Literal["000001.SH", "399001.SZ", "399006.SZ", "000680.SH"] = "000001.SH"
    as_of: date | None = None
    window: Literal[5, 10, 20, 60] = 20
    event_date: date | None = None
    view_as_of: date | None = None


class MembersQuery(ReviewQuery):
    mode: Literal["event", "countertrend"]
    sector_id: str = Field(min_length=1, max_length=64)
    snapshot_id: str = Field(min_length=1, max_length=64)
    page: int = Field(1, ge=1, le=10000)
    page_size: int = Field(50, ge=1, le=100)


class ReviewResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: Literal["ready", "partial", "unavailable"]
    context: dict
    rules_version: str
    snapshot_id: str | None = None
    rows: list[dict]
    warnings: list[str]
    coverage: dict


def _summary(request, query, mode):
    try:
        return sector_review.get_snapshot(
            request.app.state.repo, mode=mode, **query.model_dump()
        ).payload
    except sector_review.ReviewParameterError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ReviewDataUnavailableError, OSError, pl.exceptions.PolarsError) as exc:
        return {
            "status": "unavailable",
            "context": {"mode": mode, **query.model_dump(mode="json")},
            "rules_version": RULES_VERSION,
            "rows": [],
            "warnings": [
                str(exc)
                if isinstance(exc, ReviewDataUnavailableError)
                else "本地行情暂不可读，请稍后刷新"
            ],
            "coverage": {"total_sectors": 0, "evaluated_sectors": 0, "complete_sectors": 0},
            "membership_basis": "current_snapshot_backcast",
            "calculation_basis": "recomputed",
        }


@router.get("/availability")
def availability(request: Request):
    return sector_review.availability(request.app.state.repo)


@router.get("/countertrend", response_model=ReviewResponse)
def countertrend(request: Request, query: Annotated[ReviewQuery, Query()]):
    return _summary(request, query, "countertrend")


@router.get("/event", response_model=ReviewResponse)
def event(request: Request, query: Annotated[ReviewQuery, Query()]):
    return _summary(request, query, "event")


@router.get("/members")
def members(request: Request, query: Annotated[MembersQuery, Query()]):
    try:
        return sector_review.members(request.app.state.repo, **query.model_dump())
    except sector_review.ReviewSnapshotConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except sector_review.ReviewParameterError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ReviewDataUnavailableError, OSError, pl.exceptions.PolarsError) as exc:
        raise HTTPException(409, "数据已更新或暂不可读，刷新后查看") from exc
