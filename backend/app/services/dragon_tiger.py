"""龙虎榜数据服务 (fuyao 专有) — 复盘页卡片 + AI 复盘上下文。

非路由数据集: tickflow 无对应能力, 直接经 custom_sources 调 fuyao provider;
fuyao 未配置时返回 source_unavailable 状态, 前端降级提示。

数据契约 (实测 2026-08, 文档的 limit_reason/amount 实际不返回):
- 三榜一次取齐: all/org 为股票表 (org 额外带机构 4 字段), hot_money 为席位表
- 后台采集按日落 JSON 缓存 (data/dragon_tiger/date=YYYY-MM-DD.json)，包含当日结果
- 查询只读目标日缓存；尚未采集时返回 no_data，不触发供应商请求
- 未指定日期时使用北京时间当日，显式日期保持原值。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date as date_cls
from pathlib import Path

from app.market_time import cn_today

logger = logging.getLogger(__name__)

_BOARDS = ("all", "org", "hot_money")
_DATE_DIR_RE = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")


def _local_trading_days(data_dir: Path) -> list[date_cls]:
    """本地日K分区日期 = 已知交易日集合 (升序)。扫描失败返回空。"""
    root = data_dir / "kline_daily"
    out: list[date_cls] = []
    try:
        for d in root.iterdir():
            m = _DATE_DIR_RE.match(d.name)
            if d.is_dir() and m:
                try:
                    out.append(date_cls.fromisoformat(m.group(1)))
                except ValueError:
                    continue
    except OSError:
        return []
    return sorted(out)


def resolve_trade_date(data_dir: Path, target: date_cls | None) -> date_cls | None:
    """目标日 → ≤目标日的最近本地交易日。None 或本地无更早分区 → None (由 fuyao 默认)。

    目标日早于全部本地分区 (极老的历史复盘) → 原样返回, 由调用方试显式日期,
    失败如实报 no_data, 不静默给错日期的数据。
    """
    if target is None:
        return None
    days = _local_trading_days(data_dir)
    if not days:
        return target
    candidates = [d for d in days if d <= target]
    return max(candidates) if candidates else target


def _prev_trading_day(data_dir: Path, d: date_cls) -> date_cls | None:
    days = _local_trading_days(data_dir)
    earlier = [x for x in days if x < d]
    return max(earlier) if earlier else None


def _provider():
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider("fuyao"):
        return None
    return custom_sources.get_provider("fuyao")


def _fetch_boards(provider, d: date_cls) -> dict:
    """三榜取齐。任一榜失败抛 FuyaoError (整日失败, 不留半套缓存)。"""
    return {
        bt: _boards_of(provider, bt, d.isoformat()) for bt in _BOARDS
    }


def _boards_of(provider, board_type: str, iso: str | None) -> dict:
    data = provider.dragon_tiger(board_type, iso)
    return {
        "trade_date": data.get("trade_date"),
        "stock_count": data.get("stock_count"),
        "count": data.get("count"),
        "stock_items": normalize_stock_items(data.get("stock_items")),
        "hot_money_items": normalize_hot_money_items(data.get("hot_money_items")),
    }


_TICKER_RE = re.compile(r"^(\d{6})(?:\.(?:SH|SZ|BJ))?$")


def derive_thscode(ticker: object) -> str | None:
    """由 ticker 推导带市场后缀的 thscode (如 002731 → 002731.SZ)。

    fuyao 对退市整理期股 / B 股返回的 thscode 可能为 null (ticker 有值),
    不能把 null 透传给前端 (各消费方按 string 契约使用)。规则:
      6xxxxx / 900xxx (沪B)         → .SH
      0xxxxx / 3xxxxx / 200xxx(深B)  → .SZ
      4xxxxx / 8xxxxx / 920xxx (北交所) → .BJ
    """
    if not isinstance(ticker, str):
        return None
    raw = ticker.strip().upper()
    m = _TICKER_RE.match(raw)
    if not m:
        return None
    if "." in raw:
        return raw
    code = m.group(1)
    if code.startswith("900") or code[0] == "6":
        return f"{code}.SH"
    if code.startswith("200") or code[0] in ("0", "3"):
        return f"{code}.SZ"
    if code.startswith("920") or code[0] in ("4", "8"):
        return f"{code}.BJ"
    return None


def normalize_stock_items(items: object) -> list[dict]:
    """榜单个股行规范化: thscode 缺失时由 ticker 推导, 两者都不可用则丢弃并告警。"""
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    dropped: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = derive_thscode(item.get("thscode")) or derive_thscode(item.get("ticker"))
        if code is None:
            dropped.append(str(item.get("name") or item.get("ticker") or "?"))
            continue
        out.append({**item, "thscode": code})
    if dropped:
        logger.warning(
            "龙虎榜 %d 条标的缺少可识别代码 (thscode/ticker 均不可用), 已跳过: %s",
            len(dropped), dropped[:5])
    return out


def normalize_hot_money_items(items: object) -> list[dict]:
    """游资席位行: 席位本身无代码, 但关联股票 rows 必须带可用 thscode。"""
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for seat in items:
        if not isinstance(seat, dict):
            continue
        rows = seat.get("rows")
        if isinstance(rows, list):
            seat = {**seat, "rows": normalize_stock_items(rows)}
        out.append(seat)
    return out


def normalize_payload(payload: dict) -> dict:
    """规范化整个龙虎榜容器 (新拉取与历史缓存读回共用)。"""
    for board in ("all", "org"):
        b = payload.get(board)
        if isinstance(b, dict):
            payload[board] = {
                **b,
                "stock_items": normalize_stock_items(b.get("stock_items") or []),
            }
    hot = payload.get("hot_money")
    if isinstance(hot, dict):
        payload["hot_money"] = {
            **hot,
            "hot_money_items": normalize_hot_money_items(hot.get("hot_money_items") or []),
        }
    return payload


def _cache_path(data_dir: Path, d: date_cls) -> Path:
    return data_dir / "dragon_tiger" / f"date={d.isoformat()}.json"


def _load_cache(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return normalize_payload(payload) if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _store_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def get_dragon_tiger(data_dir: Path, target: date_cls | None = None) -> dict:
    """只读取后台保存的结果；当日缺失不向供应商补拉。"""
    day = target or cn_today()
    cached = _load_cache(_cache_path(data_dir, day))
    if cached is not None:
        return cached
    return {"state": "no_data", "requested_date": day.isoformat()}


def prepare_dragon_tiger(data_dir: Path, target: date_cls | None = None) -> dict:
    """预取完整三榜，不写本地缓存；任一榜失败均保留既有结果。"""
    day = target or cn_today()
    provider = _provider()
    if provider is None:
        return {"state": "source_unavailable", "requested_date": day.isoformat()}
    raw = _fetch_boards(provider, day)
    return {"state": "ok", "requested_date": day.isoformat(),
            "trade_date": raw["all"].get("trade_date"), **raw}


def commit_dragon_tiger(prepared: dict, data_dir: Path) -> dict:
    """由采集主线程统一提交三榜并通知读取方，保持上游真实日期。"""
    from app.services.shared_runtime import publish_data_update

    day = date_cls.fromisoformat(prepared["requested_date"])
    payload = prepared
    _store_cache(_cache_path(data_dir, day), payload)
    publish_data_update(data_dir, ["dragon_tiger"])
    return payload


def sync_dragon_tiger(data_dir: Path, target: date_cls | None = None) -> dict:
    """collector 显式更新三榜，包含当日结果。"""
    return commit_dragon_tiger(prepare_dragon_tiger(data_dir, target), data_dir)


def build_recap_context(data_dir: Path) -> str:
    """AI 复盘的龙虎榜摘要段 (纯文本, 失败返回空串不影响复盘)。"""
    try:
        payload = get_dragon_tiger(data_dir, None)
        if payload.get("state") not in ("ok", "fallback_prev"):
            return ""
        items = payload.get("all", {}).get("stock_items") or []
        org_items = payload.get("org", {}).get("stock_items") or []
        hm_items = payload.get("hot_money", {}).get("hot_money_items") or []
        trade_date = payload.get("trade_date") or ""
        lines = [f"(数据日期: {trade_date})"]

        top_buy = sorted(
            [i for i in items if (i.get("net_value") or 0) > 0],
            key=lambda x: x.get("net_value") or 0, reverse=True,
        )[:5]
        if top_buy:
            lines.append("净买入居前: " + "; ".join(
                f"{i.get('name')}({i.get('thscode')}) 净买{float(i.get('net_value') or 0)/1e8:.2f}亿"
                f" 涨跌{(i.get('change') or 0)*100:.1f}%"
                + (" [3日榜]" if i.get("range_days") == 3 else "")
                for i in top_buy))
        top_sell = sorted(
            [i for i in items if (i.get("net_value") or 0) < 0],
            key=lambda x: x.get("net_value") or 0,
        )[:5]
        if top_sell:
            lines.append("净卖出居前: " + "; ".join(
                f"{i.get('name')} 净卖{abs(float(i.get('net_value') or 0))/1e8:.2f}亿"
                for i in top_sell))
        if org_items:
            lines.append("机构净买居前: " + "; ".join(
                f"{i.get('name')} 机构净买{float(i.get('org_net_value') or 0)/1e8:.2f}亿"
                for i in sorted(org_items, key=lambda x: x.get("org_net_value") or 0, reverse=True)[:5]))
        if hm_items:
            lines.append("活跃游资: " + "; ".join(
                f"{h.get('name')} 净买{float(h.get('buying') or 0)/1e8:.2f}亿"
                for h in sorted(hm_items, key=lambda x: x.get('buying') or 0, reverse=True)[:5]))
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001 — 摘要失败不影响复盘主流程
        logger.debug("龙虎榜复盘摘要构建失败: %s", e)
        return ""
