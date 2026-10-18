"""共享交易日历：采集服务更新，所有读取和调度判断只访问本地文件。"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from app.market_time import cn_now

logger = logging.getLogger(__name__)


def _path(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        from app.config import settings
        data_dir = settings.data_dir
    return data_dir / "calendar" / "trading_days.json"


def sync_calendar(data_dir: Path | None = None) -> dict:
    """collector 显式更新 fuyao 日历；不可用时保留已保存日历，不猜测交易日。"""
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider("fuyao"):
        return {"updated": False, "reason": "交易日历数据源未配置"}
    days = custom_sources.get_provider("fuyao").trading_days()
    if not days:
        return {"updated": False, "reason": "交易日历数据源未返回日期"}
    now = cn_now()
    payload = {
        "days": sorted(d.isoformat() for d in days),
        "start": min(days).isoformat(),
        # 日历端点提供近一年截至当日的日期；当日缺席即休市。
        "end": max(now.date(), max(days)).isoformat(),
        "updated_at": now.isoformat(),
    }
    previous = calendar_status(data_dir)
    # 供应商滚动一年窗口不能删掉已经确认的事件交易日；只合并重叠覆盖，
    # 长期停采形成的未知区间不据此假装连续已知。
    if (previous and payload["start"] <= previous["end"]
            and previous["start"] <= payload["end"]):
        payload["days"] = sorted(set(previous["days"]) | set(payload["days"]))
        payload["start"] = min(previous["start"], payload["start"])
        payload["end"] = max(previous["end"], payload["end"])
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return {"updated": True, "start": payload["start"], "end": payload["end"]}


def calendar_status(data_dir: Path | None = None) -> dict:
    try:
        return json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def is_trading_day(now: datetime | None = None, *, data_dir: Path | None = None) -> bool | None:
    """True=交易日，False=日历确认休市，None=本地日历尚未覆盖。绝不联网。"""
    day = (now or cn_now()).date()
    calendar = calendar_status(data_dir)
    if not calendar or not (date.fromisoformat(calendar["start"]) <= day <= date.fromisoformat(calendar["end"])):
        return None
    return day.isoformat() in calendar["days"]
