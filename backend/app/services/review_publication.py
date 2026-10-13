"""盘后市场复盘快照，与可选 AI 文字报告独立。"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.market_time import cn_now, cn_today


def get_publication(data_dir: Path, day: date | None = None) -> dict:
    day = day or cn_today()
    path = data_dir / "market_recap" / f"date={day.isoformat()}.json"
    if not path.exists():
        return {"as_of": day.isoformat(), "published": False, "published_at": None, "overview": None}
    return json.loads(path.read_text(encoding="utf-8"))


def publish_review(repo, day: date, quote_service=None, depth_service=None) -> dict:
    from app.services.market_overview_builder import _json_safe, build_market_overview
    from app.services.shared_runtime import publish_data_update

    overview = build_market_overview(repo, quote_service, depth_service, day)
    payload = {"as_of": day.isoformat(), "published": True,
               "published_at": cn_now().isoformat(), "overview": _json_safe(overview)}
    path = repo.store.data_dir / "market_recap" / f"date={day.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str), encoding="utf-8")
    tmp.replace(path)
    publish_data_update(repo.store.data_dir, ["review"])
    return payload


def publish_review_if_ready(repo, quote_service=None, depth_service=None) -> dict | None:
    """仅发布已经落盘的交易日期；今日盘中同步不提前发布盘后复盘。"""
    from datetime import time
    _, day = repo.get_enriched_latest()
    now = cn_now()
    if day is None or day > now.date() or (day == now.date() and now.time() < time(15)):
        return None
    return publish_review(repo, day, quote_service, depth_service)
