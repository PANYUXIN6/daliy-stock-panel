"""K 线 / 同步 API。"""
from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import gzip
import json
import logging
import math
from contextlib import suppress
from datetime import date, timedelta
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.db_safe import is_valid_ext_ident
from app.market_time import cn_today
from app.price_limits import (
    is_no_limit_day,
    is_risk_warning_name,
    parse_listing_date,
    price_limit_pct,
)
from app.services import kline_sync, minute_adjust, trading_day
from app.services.ordinary_minute import minute_allowed as _minute_allowed
from app.services.pipeline_jobs import JobBusyError

logger = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()

router = APIRouter(prefix="/api/kline", tags=["kline"])


def _json_safe(obj):
    """把 nan/inf 换成 None, 保证 JSON 合法。

    gzip 路径原先 allow_nan=True, 会写出前端 JSON.parse 不能吃的 NaN/Infinity;
    未压缩路径走 Starlette allow_nan=False, 遇到非有限浮点整段 500。
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _gzip_payload(request: Request, payload: dict, *, pref_key: str) -> dict | Response:
    """大 JSON 响应的传输压缩: 偏好开启 + 客户端接受 gzip + 响应超阈值才压。

    分时/日K各自使用独立偏好键 (沿用已有 *_batch_compress 存储键保证兼容)。
    level 6 实测 13MB ≈ 290ms CPU 压掉 87%; level 9 要 2.5s 不可用。
    datetime → isoformat, 与 FastAPI jsonable_encoder 输出一致
    (前端 since 增量按字符串字典序比较, 格式必须与非压缩路径相同)。
    """
    from app.services import preferences as _prefs
    _getters = {
        "minute_batch_compress": _prefs.get_minute_batch_compress,
        "daily_batch_compress": _prefs.get_daily_batch_compress,
    }
    getter = _getters.get(pref_key)
    compress_on = False
    if getter is not None:
        try:
            compress_on = bool(getter())
        except Exception:  # 偏好读取异常按不压缩返回原样
            logger.exception("gzip preference read failed")
            compress_on = False
    payload = _json_safe(payload)
    headers = getattr(request, "headers", None) or {}
    if compress_on and "gzip" in (headers.get("accept-encoding") or ""):
        raw = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            default=lambda o: o.isoformat() if hasattr(o, "isoformat") else str(o),
        ).encode()
        if len(raw) > 1024:
            return Response(
                content=gzip.compress(raw, 6),
                media_type="application/json",
                headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
            )
    return payload


@lru_cache(maxsize=8192)
def _name_pinyin_keys(name: str) -> tuple[str, ...]:
    """返回中文名称所有可能的拼音首字母串 (多音字展开为笛卡尔积)。

    '平安银行' -> ('PAYH',); '重庆百货' -> ('CQBH', 'CQMH', 'ZQBH', 'ZQMH')。
    非汉字字符原样保留: '万科A' -> ('WKA',)。
    股票名总量有限且不变, lru_cache 命中后单次查询 ≈ dict 查找, 全市场遍历 < 1ms。
    """
    from pypinyin import Style, pinyin
    if not name:
        return ()
    keys = [""]
    for group in pinyin(name, style=Style.FIRST_LETTER, heteronym=True):
        keys = [k + g.upper() for k in keys for g in group]
    return tuple(keys)


def _init_pinyin_dict() -> None:
    """加载 A 股高频多音字地名/词组词典, 使常见误读也能命中。

    pypinyin 默认词典对部分地名取常见读音 (如「重」→ chóng), 补充后「重庆」
    同时接受 zhòng/qìng (zq) 与 chóng/qīng (cq) 两种首字母, 与同花顺行为一致。
    幂等: 多次调用安全。
    """
    try:
        from pypinyin import load_phrases_dict
        # value 用二维 list: 每个字给一个或多个读音
        load_phrases_dict({
            "重庆": [["zhòng", "chóng"], ["qīng"]],
            "长安": [["cháng", "zhǎng"], ["ān"]],
            "长春": [["cháng", "zhǎng"], ["chūn"]],
            "长沙": [["cháng", "zhǎng"], ["shā"]],
            "长城": [["cháng", "zhǎng"], ["chéng"]],
            "长江": [["cháng", "zhǎng"], ["jiāng"]],
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("pypinyin phrases dict load failed (polyphone coverage may degrade): %s", exc)


_init_pinyin_dict()


def _match_pinyin(name: str, keyword: str) -> bool:
    """keyword 是否匹配 name 任一拼音首字母串的前缀 (支持多音字)。"""
    return any(k.startswith(keyword) for k in _name_pinyin_keys(name))


@router.get("/instruments/search")
def search_instruments(
    request: Request,
    q: str = Query("", min_length=0, max_length=50, description="搜索关键词"),
    limit: int = Query(20, ge=1, le=50),
    asset_types: str = Query("stock", description="逗号分隔的资产类型: stock,etf"),
):
    """模糊搜索标的 (代码 / 名称)。从内存 instruments 缓存中查。

    默认只搜股票, 保持既有调用方行为不变; 自选等场景传 asset_types=stock,etf
    可一并搜出 ETF, 结果附带 asset_type 字段供前端区分。
    """
    if not q.strip():
        return {"results": []}

    repo = request.app.state.repo
    import polars as pl

    types = [t.strip() for t in asset_types.split(",") if t.strip()]
    parts: list[pl.DataFrame] = []
    for t in types:
        df_t = repo.get_instruments_asset(t)
        if df_t.is_empty() or "symbol" not in df_t.columns:
            continue
        # dtype 全部归一到 Utf8: 股票/ETF 两份缓存来源不同 (ETF 含 legacy 合并), 防 concat SchemaError
        parts.append(df_t.with_columns([
            pl.col("symbol").cast(pl.Utf8).alias("symbol"),
            (pl.col("name").cast(pl.Utf8) if "name" in df_t.columns else pl.lit("")).alias("name"),
            (pl.col("code").cast(pl.Utf8) if "code" in df_t.columns else pl.lit("")).alias("code"),
            pl.lit(t).alias("asset_type"),
        ]).select(["symbol", "name", "code", "asset_type"]))
    if not parts:
        return {"results": []}
    df = pl.concat(parts, how="vertical")

    keyword = q.strip().upper()
    is_pinyin_query = keyword.isalpha() and keyword.isascii()

    # code/symbol 前缀优先，再 name 包含匹配
    prefix_mask = (
        pl.col("code").str.starts_with(keyword)
        | pl.col("symbol").str.to_uppercase().str.starts_with(keyword)
    )
    contains_mask = (
        pl.col("code").str.contains(keyword, literal=True)
        | pl.col("symbol").str.to_uppercase().str.contains(keyword, literal=True)
        | pl.col("name").str.contains(keyword, literal=True)
    )

    # 分层匹配: ① code/symbol 前缀 → ② 拼音首字母前缀(纯字母输入) → ③ 包含匹配
    prefix_hits = df.filter(prefix_mask).head(limit)
    if prefix_hits.height >= limit:
        matched = prefix_hits
    else:
        collected = [prefix_hits] if prefix_hits.height else []
        seen = set(prefix_hits["symbol"].to_list()) if prefix_hits.height else set()
        remaining = limit - prefix_hits.height

        # ② 拼音首字母前缀: 仅纯字母输入触发 (如 payh → 平安银行); 中文/代码输入零开销跳过
        if is_pinyin_query and remaining > 0:
            pinyin_rows = []
            for row in df.filter(~pl.col("symbol").is_in(seen)).iter_rows(named=True):
                if _match_pinyin(row["name"], keyword):
                    pinyin_rows.append(row)
                    if len(pinyin_rows) >= remaining:
                        break
            if pinyin_rows:
                collected.append(pl.DataFrame(pinyin_rows))
                seen.update(r["symbol"] for r in pinyin_rows)
                remaining -= len(pinyin_rows)

        # ③ 包含匹配补充
        if remaining > 0:
            contain_hits = df.filter(contains_mask & ~pl.col("symbol").is_in(seen)).head(remaining)
            if contain_hits.height:
                collected.append(contain_hits)

        matched = (
            pl.concat(collected, how="vertical") if len(collected) > 1
            else (collected[0] if collected else df.head(0))
        )
    rows = matched.select(["symbol", "name", "code", "asset_type"]).to_dicts()
    return {"results": rows}


@router.post("/instruments/names")
def instruments_names(request: Request, symbols: list[str]):
    """批量查标的名称 (股票 + ETF + 指数)。传入 symbol 列表, 返回 {symbol: name}。"""
    if not symbols:
        return {"names": {}}
    repo = request.app.state.repo
    return {"names": repo.get_name_map(symbols)}


def _get_stock_info(repo, symbol: str) -> dict:
    """从 instruments 内存缓存查标的名称 + 股本。

    该接口在个股弹窗打开时每秒被调用 (SSE invalidate 触发重拉), 走
    repo.get_instruments() 的 Polars 内存缓存按 symbol 过滤, 不再每请求
    DuckDB 扫 instruments parquet。列缺失时返回空 dict, 与旧 SQL 报错路径一致。
    """
    import polars as pl
    try:
        df = repo.get_instruments()
        needed = ("symbol", "name", "total_shares", "float_shares")
        if df.is_empty() or not all(c in df.columns for c in needed):
            return {}
        hit = df.filter(pl.col("symbol") == symbol).head(1)
        if hit.is_empty():
            return {}
        return {
            "name": hit["name"][0],
            "total_shares": hit["total_shares"][0],
            "float_shares": hit["float_shares"][0],
        }
    except Exception:  # noqa: BLE001
        return {}


def _get_asset_info(repo, symbol: str, asset_type: str) -> dict:
    """非股票标的 (ETF / 指数) 的名称信息 — 从对应 instruments 缓存查, 无股本概念。"""
    import polars as pl
    try:
        df = repo.get_instruments_asset(asset_type)
        if df.is_empty() or "symbol" not in df.columns or "name" not in df.columns:
            return {}
        hit = df.filter(pl.col("symbol") == symbol).head(1)
        if hit.is_empty():
            return {}
        return {"name": hit["name"][0]}
    except Exception:
        logger.exception("asset information read failed: %s", symbol)
        return {}


def _get_price_limit_info(
    repo,
    symbol: str,
    trade_date: date,
    asset_type: str,
    instrument_name: str | None,
) -> dict | None:
    """Return the date-aware limit rule and today's authoritative prices."""
    if asset_type == "index":
        return None

    info: dict = {
        "rate": price_limit_pct(
            symbol,
            trade_date,
            is_risk_warning=(
                asset_type == "stock" and is_risk_warning_name(instrument_name)
            ),
        ),
        "limit_up": None,
        "limit_down": None,
        "no_limit": False,
        "source": "rule",
    }

    # instrument 行一次取出: 今日权威涨跌停价 + listing_date 窗口判定共用
    row: dict | None = None
    try:
        import polars as pl

        instruments = repo.get_instruments_asset(asset_type)
        available = [
            column
            for column in ("symbol", "limit_up", "limit_down", "listing_date")
            if column in instruments.columns
        ]
        if "symbol" in available and len(available) > 1:
            hit = instruments.filter(pl.col("symbol") == symbol).select(available).head(1)
            if not hit.is_empty():
                row = hit.to_dicts()[0]
    except Exception:
        logger.exception("price limit information read failed: %s", symbol)
        row = None

    # 注册制新股上市初期无涨跌幅: listing_date 命中窗口时 no_limit=True,
    # 压过 rate 与维表值 (前端不再画涨跌停带, y 轴按实际数据自适应)
    if row is not None:
        listing = parse_listing_date(row.get("listing_date"))
        if listing is not None and is_no_limit_day(symbol, listing, trade_date):
            info["no_limit"] = True
            return info

    if trade_date != cn_today():
        return info

    if row is None:
        return info

    has_authoritative_price = False
    for field in ("limit_up", "limit_down"):
        value = row.get(field)
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric) and 0 < numeric < 10_000:
            info[field] = numeric
            has_authoritative_price = True
    if has_authoritative_price:
        info["source"] = "instrument"
    return info


def _get_previous_closes(
    repo,
    symbol: str,
    trade_dates: list[date],
    asset_type: str,
) -> dict[date, float | None]:
    """Return the previous trading day's adjusted close for each session."""
    if not trade_dates:
        return {}
    start = min(trade_dates) - timedelta(days=45)
    end = max(trade_dates)
    try:
        daily = repo.get_daily_asset(
            asset_type,
            symbol,
            start,
            end,
            columns=["date", "close"],
        ).sort("date")
    except Exception:
        logger.exception("previous close read failed: %s", symbol)
        daily = None
    if daily is None or daily.is_empty():
        return {trade_date: None for trade_date in trade_dates}

    closes: list[tuple[date, float]] = []
    for daily_date, close in daily.select(["date", "close"]).iter_rows():
        if close is None:
            continue
        numeric = float(close)
        if math.isfinite(numeric) and numeric > 0:
            closes.append((daily_date, numeric))

    result: dict[date, float | None] = {}
    for trade_date in trade_dates:
        result[trade_date] = next(
            (close for daily_date, close in reversed(closes) if daily_date < trade_date),
            None,
        )
    return result


@router.get("/daily")
def get_daily(
    request: Request,
    symbol: str = Query(..., description="标的代码,如 000001.SZ"),
    days: int = Query(120, ge=10, le=2000),
    start_date: str | None = Query(None, description="起始日期 YYYY-MM-DD, 优先于 days"),
    end_date: str | None = Query(None, description="截止日期 YYYY-MM-DD, 默认今天"),
    ext_columns: str | None = Query(None, description="逗号分隔的 ext 列: config_id.field_name"),
):
    """读取本地 enriched 表中某只股票的日 K。

    - 请求区间包含今天且有实时行情时, 追加/覆盖今日实时蜡烛
    - 本地没有该标的数据时返回空结果，由显式同步或后台采集更新
    - ext_columns: 可选，动态 LEFT JOIN 扩展数据表，结果平铺到 stock_info.ext 下
      (key 为 "{config_id}__{field_name}")，供日K信息条等场景展示自定义字段
    """

    repo = request.app.state.repo
    # 未传 end_date 时用北京今天: 实时注入只在内存缓存命中时补当日 K,
    # 缓存冷时 parquet 当日行能否进结果取决于这个窗口右端。
    end = date.fromisoformat(end_date) if end_date else cn_today()
    start = date.fromisoformat(start_date) if start_date else end - timedelta(days=days)

    asset_type = repo.resolve_asset_type(symbol)
    stock_info = _get_stock_info(repo, symbol) if asset_type == "stock" else _get_asset_info(repo, symbol, asset_type)
    stock_name = stock_info.get("name")

    # 从 enriched 表读取 (已含前复权 OHLCV + 技术指标 + 信号); ETF/指数走独立存储
    df = repo.get_daily_asset(asset_type, symbol, start, end)

    if df.is_empty():
        return _gzip_payload(
            request,
            _attach_ext({"symbol": symbol, "name": stock_name, "stock_info": stock_info,
                         "rows": [], "source": "none"}, repo, symbol, ext_columns),
            pref_key="daily_batch_compress",
        )

    rows = df.to_dicts()

    # 追加/覆盖今日实时蜡烛
    rows = _maybe_inject_live_candle(request, symbol, rows, asset_type, start=start, end=end)

    resp = {"symbol": symbol, "name": stock_name, "stock_info": stock_info, "rows": rows, "source": "enriched"}
    return _gzip_payload(
        request,
        _attach_ext(resp, repo, symbol, ext_columns),
        pref_key="daily_batch_compress",
    )


def _attach_ext(resp: dict, repo, symbol: str, ext_columns: str | None) -> dict:
    """按 ext_columns 规格为单只股票 LEFT JOIN 扩展数据，平铺到 stock_info['ext']。

    key 形如 "{config_id}__{field_name}"，与自选列表 enriched 接口保持一致。
    委托 screener._load_ext_value_maps 取值: 复用其 (路径,mtime) 签名缓存,
    个股弹窗每秒重拉时不再重复读 ext parquet; 任何 ext 表/字段缺失都静默跳过。
    """
    if not ext_columns or not ext_columns.strip():
        return resp

    specs: list[tuple[str, str]] = []
    for part in ext_columns.split(","):
        part = part.strip()
        if "." not in part:
            continue
        config_id, field_name = part.split(".", 1)
        config_id, field_name = config_id.strip(), field_name.strip()
        if config_id and field_name and is_valid_ext_ident(config_id):
            specs.append((config_id, field_name))
    if not specs:
        return resp

    try:
        from app.api.screener import _load_ext_value_maps
        value_maps = _load_ext_value_maps(repo, ext_columns)
    except Exception:  # noqa: BLE001
        value_maps = {}

    ext_values: dict = {}
    for config_id, field_name in specs:
        ext_col_name = f"{config_id}__{field_name}"
        vmap = value_maps.get(ext_col_name) or {}
        ext_values[ext_col_name] = vmap.get(symbol)

    stock_info = dict(resp.get("stock_info") or {})
    stock_info["ext"] = ext_values
    resp["stock_info"] = stock_info
    return resp


def _latest_live_candle(
    request: Request,
    symbol: str,
    asset_type: str = "stock",
    *,
    refresh_asset: bool = True,
) -> dict | None:
    """从内存缓存读取单只标的的当日实时 enriched 行。"""

    if asset_type == "stock":
        qs = getattr(request.app.state, "quote_service", None)
        if not qs:
            return None
        df_today, enriched_date = qs.get_enriched_today()
    elif asset_type in {"etf", "index"}:
        df_today, enriched_date = request.app.state.repo.get_enriched_latest_asset(
            asset_type, refresh=refresh_asset,
        )
    else:
        return None
    if df_today.is_empty():
        return None

    # 非交易日(周末/假日)缓存日期 != 北京今天, 跳过注入避免产生重复蜡烛。
    # 必须用 cn_today(): 美洲时区主机整个 A 股交易时段本地日期落后北京一天,
    # 旧代码盘中直接丢K。UTC 主机盘中(UTC 1:30-7:00)本地日期与北京相同, 并不丢K;
    # UTC 的旧症状是北京 00:00-08:00 把昨日残留快照误当实时K注入。
    if not enriched_date or enriched_date != cn_today():
        return None

    # 查找该 symbol 的实时 enriched 行
    import polars as pl
    try:
        q = df_today.filter(pl.col("symbol") == symbol).to_dicts()
        if not q:
            return None
        q = q[0]
    except Exception:
        logger.exception("live candle read failed: %s", symbol)
        return None

    close_price = q.get("close")
    if not close_price or close_price <= 0:
        return None

    # 沿用完整日K接口原有的实时行投影, 避免增量接口形成第二套字段契约。
    # API 在非交易时段可能返回 open/high/low=0, 用 close 填充避免异常蜡烛。
    raw_open = q.get("open")
    raw_high = q.get("high")
    raw_low = q.get("low")
    live_row = {
        "date": str(enriched_date),
        "symbol": symbol,
        "open": raw_open if raw_open and raw_open > 0 else close_price,
        "high": raw_high if raw_high and raw_high > 0 else close_price,
        "low": raw_low if raw_low and raw_low > 0 else close_price,
        "close": close_price,
        "volume": q.get("volume"),
        "amount": q.get("amount"),
        "change_pct": q.get("change_pct"),
        "is_live": True,
    }
    for key in ("ma5", "ma10", "ma20", "ma30", "ma60", "bull_bear_line",
                "macd_dif", "macd_dea", "macd_hist",
                "kdj_k", "kdj_d", "kdj_j",
                "boll_upper", "boll_lower",
                "rsi_6", "rsi_14", "rsi_24",
                "atr_14", "vol_ratio_5d"):
        if key in q and q[key] is not None:
            live_row[key] = q[key]
    return live_row


def _maybe_inject_live_candle(
    request: Request,
    symbol: str,
    rows: list[dict],
    asset_type: str = "stock",
    *,
    start: date,
    end: date,
) -> list[dict]:
    """区间包含今天时, 用当日实时 enriched 数据追加/覆盖今日蜡烛。"""
    live_row = _latest_live_candle(request, symbol, asset_type)
    if live_row is None or not start <= date.fromisoformat(live_row["date"]) <= end:
        return rows

    # 如果已有今天的 enriched 行, 覆盖; 否则追加
    found = False
    for r in rows:
        if str(r.get("date")) == live_row["date"]:
            r.update(live_row)
            found = True
            break

    if not found:
        rows.append(live_row)

    return rows


@router.get("/gaps")
def get_price_gaps(
    request: Request,
    symbol: Annotated[str, Query(pattern=r"^\d{6}\.(SH|SZ|BJ)$")],
    end_date: date,
):
    """按指定截止日返回全部本地未回补价格缺口，包含实时尾部。"""
    import polars as pl

    from app.services.price_gaps import get_price_gaps as build
    repo = request.app.state.repo
    asset = repo.resolve_asset_type(symbol)
    live = _latest_live_candle(request, symbol, asset, refresh_asset=False) if end_date >= cn_today() else None
    try:
        return build(repo, symbol, min(end_date, cn_today()), live_row=live)
    except (OSError, pl.exceptions.PolarsError, ValueError) as exc:
        logger.warning("缺口区域本地日线读取失败: %s", symbol, exc_info=True)
        raise HTTPException(503, "缺口区域日线读取失败，请检查已保存数据") from exc


@router.get("/earnings-breakout")
def get_earnings_breakout(
    request: Request,
    symbol: Annotated[str, Query(pattern=r"^\d{6}\.(SH|SZ|BJ)$")],
    start_date: date,
    end_date: date,
):
    """正式财报版主图指标，只读已保存版本与本地行情。"""
    import polars as pl

    from app.services.earnings_breakout import get_earnings_breakout as build
    repo = request.app.state.repo
    if start_date > end_date:
        raise HTTPException(400, "起始日期不能晚于截止日期")
    if repo.resolve_asset_type(symbol) != "stock":
        return {"symbol": symbol, "source": "", "status": "unsupported", "reports": 0,
                "eligible_reports": 0, "events": [], "markers": []}
    live = _latest_live_candle(request, symbol, "stock", refresh_asset=False) if end_date >= cn_today() else None
    try:
        return build(repo, symbol, start_date, min(end_date, cn_today()), live_row=live)
    except (OSError, pl.exceptions.PolarsError, ValueError) as exc:
        logger.warning("业绩断层本地数据读取失败: %s", symbol, exc_info=True)
        raise HTTPException(503, "财务版本或交易日历读取失败，请检查已保存数据") from exc


@router.get("/daily/latest")
def get_daily_latest(
    request: Request,
    symbol: str = Query(..., description="标的代码,如 000001.SZ"),
):
    """返回内存中的当日单行 K 线, 供详情页实时增量更新。"""
    repo = request.app.state.repo
    asset_type = repo.resolve_asset_type(symbol)
    row = _latest_live_candle(request, symbol, asset_type, refresh_asset=False)
    return {
        "symbol": symbol,
        "row": row,
        "source": "live" if row is not None else "none",
    }


class DailyBatchRequest:
    """批量日K请求。"""
    symbols: list[str]
    days: int = 12


@router.post("/daily-batch")
def get_daily_batch(request: Request, body: dict):
    """批量获取多只股票最近 N 天日K (OHLCV)。

    用于自选列表迷你蜡烛图等场景，只返回基础列，不返回全部 enriched 指标。
    """
    symbols = body.get("symbols", [])
    days = body.get("days", 12)
    if not symbols:
        return {"data": {}}
    days = max(5, min(60, days))

    repo = request.app.state.repo
    from datetime import timedelta

    import polars as pl

    # 窗口右端必须是北京今天: QuoteService 当日 flush 的分区日期是北京交易日。
    # 美西主机整个 A 股交易时段、UTC 主机北京 00:00-08:00, date.today() 比北京早一天,
    # 迷你蜡烛会把当日实时 K 排除在窗口外。
    end = cn_today()
    start = end - timedelta(days=days * 2)  # 多取一些确保交易日够

    cols = ["symbol", "date", "open", "high", "low", "close", "volume"]

    # 按资产类型分组: stock 走批量缓存; etf/index 逐只查独立存储 (数量少, 成本可忽略)
    stock_symbols: list[str] = []
    etf_symbols: list[str] = []
    index_symbols: list[str] = []
    for s in symbols:
        t = repo.resolve_asset_type(s)
        if t == "etf":
            etf_symbols.append(s)
        elif t == "index":
            index_symbols.append(s)
        else:
            stock_symbols.append(s)

    frames: list[pl.DataFrame] = []
    if stock_symbols:
        df_stock = repo.get_daily_batch(stock_symbols, start, end, columns=cols)
        if not df_stock.is_empty():
            frames.append(df_stock)
    for sym in etf_symbols:
        sub = repo.get_etf_daily(sym, start, end, columns=cols)
        if not sub.is_empty():
            frames.append(sub)
    for sym in index_symbols:
        sub = repo.get_index_daily(sym, start, end, columns=cols)
        if not sub.is_empty():
            frames.append(sub)

    if not frames:
        return {"data": {}}
    df = pl.concat(frames, how="diagonal_relaxed")

    # 按 symbol 分组, 每只取最近 N 条。
    # partition_by 一次切分, 避免 N 只自选时对同一批数据做 N 次全帧过滤。
    result: dict[str, list[dict]] = {}
    for part in df.partition_by("symbol", maintain_order=True):
        sub = part.sort("date").tail(days)
        if not sub.is_empty():
            result[sub["symbol"][0]] = sub.to_dicts()

    # 日K批量同为大响应端点 (千只自选 MB 级), 与分时各自独立压缩开关
    return _gzip_payload(request, {"data": result}, pref_key="daily_batch_compress")


@router.post("/minute-batch")
def get_minute_batch(request: Request, body: dict):
    """批量读取后台已落盘的分钟K；缺口保持空态，浏览不触发补数。"""
    from datetime import datetime

    import polars as pl

    symbols: list[str] = body.get("symbols", [])
    if not symbols:
        return {"data": {}}
    repo = request.app.state.repo
    day = date.fromisoformat(body["date"]) if body.get("date") else cn_today()

    since = None
    if body.get("since"):
        try:
            since = datetime.fromisoformat(str(body["since"]))
            if since.tzinfo is not None:
                since = since.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        except ValueError:
            since = None

    etfs = repo.get_etf_symbol_set()
    indices = repo.get_index_symbol_set()
    groups = {
        "stock": [s for s in symbols if s not in etfs and s not in indices],
        "etf": [s for s in symbols if s in etfs],
        "index": [s for s in symbols if s in indices],
    }
    if not body.get("date") and trading_day.is_trading_day() is not True:
        dates = [repo.latest_minute_date_global(asset_type=asset)
                 for asset, members in groups.items() if members]
        day = max((d for d in dates if d is not None), default=None) or repo.latest_daily_date() or day
    result = {}
    for asset, members in groups.items():
        if not members:
            continue
        df = repo.get_minute_batch(members, day, asset_type=asset)
        if df.is_empty():
            continue
        if since is not None:
            df = df.filter(pl.col("datetime") >= since)
        for part in df.partition_by("symbol", maintain_order=True):
            if not part.is_empty():
                result[part["symbol"][0]] = part.sort("datetime").to_dicts()
    return _gzip_payload(
        request, {"data": result, "full_minute_local": True, "incremental": since is not None},
        pref_key="minute_batch_compress",
    )


@router.get("/minute-range")
def get_minute_range(
    request: Request,
    symbol: str = Query(..., description="标的代码"),
    days: int = Query(10, ge=1, le=20, description="最近交易日数量"),
):
    """读取单只标的最近 N 个已落库交易日的分钟 K。"""
    import polars as pl

    repo = request.app.state.repo
    asset_type = repo.resolve_asset_type(symbol)
    stock_info = (
        _get_stock_info(repo, symbol)
        if asset_type == "stock"
        else _get_asset_info(repo, symbol, asset_type)
    )
    base_response = {
        "symbol": symbol,
        "name": stock_info.get("name"),
        "asset_type": asset_type,
        "requested_days": days,
    }

    # 多日端点保留股票/ETF范围；指数单日由 /api/index/minute 读取后台已落盘结果。
    if asset_type == "index":
        return _gzip_payload(
            request,
            {**base_response, "sessions": [], "source": "none"},
            pref_key="minute_batch_compress",
        )

    end = cn_today()
    start = end - timedelta(days=days * 3 + 20)
    minute = repo.get_minute_range([symbol], start, end, asset_type=asset_type)
    if minute.is_empty() or "datetime" not in minute.columns:
        return _gzip_payload(
            request,
            {**base_response, "sessions": [], "source": "none"},
            pref_key="minute_batch_compress",
        )

    minute = minute.with_columns(
        pl.col("datetime").dt.date().alias("_trade_date"),
    )
    trade_dates = sorted(minute["_trade_date"].unique().to_list())[-days:]
    previous_closes = _get_previous_closes(repo, symbol, trade_dates, asset_type)
    row_columns = [
        column
        for column in (
            "datetime", "open", "high", "low", "close", "volume", "amount"
        )
        if column in minute.columns
    ]
    sessions = []
    for trade_date in trade_dates:
        rows = (
            minute.filter(pl.col("_trade_date") == trade_date)
            .sort("datetime")
            .select(row_columns)
            .to_dicts()
        )
        if rows:
            sessions.append({
                "date": trade_date.isoformat(),
                "prev_close": previous_closes.get(trade_date),
                "rows": rows,
            })

    return _gzip_payload(
        request,
        {
            **base_response,
            "sessions": sessions,
            "source": "local" if sessions else "none",
        },
        pref_key="minute_batch_compress",
    )


@router.get("/minute")
def get_minute(
    request: Request,
    symbol: str = Query(..., description="标的代码"),
    trade_date: Annotated[date | None, Query(alias="date", description="交易日期, 默认最新")] = None,
    live: bool = Query(False, description="读取后台最新本地结果"),
):
    """读取单只标的的本地分钟K；保留 live 参数兼容已有客户端。"""
    repo = request.app.state.repo
    asset_type = repo.resolve_asset_type(symbol)
    stock_info = _get_stock_info(repo, symbol) if asset_type == "stock" else _get_asset_info(repo, symbol, asset_type)
    stock_name = stock_info.get("name")
    if trade_date is None:
        trade_date = cn_today()
        if trading_day.is_trading_day() is not True:
            trade_date = repo.latest_minute_date(symbol, asset_type=asset_type) or repo.latest_daily_date() or trade_date
    df = repo.get_minute(symbol, trade_date, asset_type=asset_type)
    return _gzip_payload(
        request,
        {"symbol": symbol, "name": stock_name, "stock_info": stock_info,
         "date": str(trade_date), "rows": df.to_dicts(), "source": "local" if not df.is_empty() else "none",
         "asset_type": asset_type,
         "price_limit": _get_price_limit_info(repo, symbol, trade_date, asset_type, stock_name),
         "prev_close": _get_previous_closes(repo, symbol, [trade_date], asset_type).get(trade_date)},
        pref_key="minute_batch_compress",
    )


async def _sync_daily_job(request: Request, symbols: list[str], days: int) -> dict:
    from app.api.pipeline import _submit_job
    repo = request.app.state.repo
    capset = request.app.state.capabilities

    def make_run(progress):
        def run():
            progress("sync_daily", 0, "同步日线…")
            zero = []
            rows = kline_sync.sync_and_persist_daily_batch(
                symbols, repo, capset, count=days, zero_row_out=zero,
                on_chunk_done=lambda cur, total: progress("sync_daily", int(cur * 95 / max(total, 1)), f"日线 {cur}/{total}"),
            )
            progress("done", 100, f"日线完成，写入 {rows} 行")
            return {"symbols": symbols, "rows_written": rows, "zero_row_symbols": zero}
        return run

    return await _submit_job(repo, getattr(request.app.state, "quote_service", None), make_run,
                             request_key=f"daily:{','.join(sorted(set(symbols)))}:{days}", datasets=["daily"])


@router.post("/sync")
async def sync_symbol(request: Request, symbol: str, days: int = Query(250, ge=10, le=2000)):
    return {"status": "started", "symbol": symbol, **await _sync_daily_job(request, [symbol], days)}


@router.post("/sync_batch")
async def sync_batch(request: Request, symbols: list[str], days: int = Query(250, ge=10, le=2000)):
    return {"status": "started", "symbols": symbols, **await _sync_daily_job(request, symbols, days)}


@router.post("/refresh_views")
def refresh_views(request: Request):
    """刷新所有 DuckDB 视图(解决视图状态不一致问题)。"""
    from app.jobs.daily_pipeline import _refresh_views
    repo = request.app.state.repo
    _refresh_views(repo)
    return {"status": "ok"}


@router.post("/sync_minute")
async def sync_minute(request: Request):
    """手动触发分钟 K 同步(全市场)。返回 pipeline job_id 可轮询进度。

    body 可选: { "days": int, "extend": bool }。
    普通同步首次回溯 days 自然日，已有数据从最新时间增量更新；不传则用偏好设置。
    extend=True（或 days>=365）从本地最早时间向前扩展约 days 个交易日，
    无本地数据则从当前时间回溯；区间采用自然日估算，不保证精确交易日数量。
    """
    # 可选 body: { "days": int, "extend": bool }
    # days 的单位依模式而定；extend 从最早数据向前按交易日数量估算窗口。
    body = {}
    with suppress(Exception):
        body = await request.json()
    return await trigger_minute_sync(
        request.app.state.repo,
        request.app.state.capabilities,
        override_days=body.get("days"),
        extend_flag=body.get("extend"),
    )


async def trigger_minute_sync(repo, capset, *, override_days=None, extend_flag=None) -> dict:
    """触发分钟K同步/向前扩展后台任务(HTTP 端点与 AI 助手共用同一条触发路径)。"""
    import asyncio

    from app.api.data import invalidate_storage_cache
    from app.services.collection_status import collection_context
    from app.services.pipeline_jobs import (
        JobCancelledError,
        job_store,
        release_run_slot,
        run_with_capacity,
        try_acquire_run_slot,
    )
    from app.services.preferences import get_minute_sync_days
    from app.tickflow.pools import get_pool

    if not _minute_allowed(capset):
        raise HTTPException(status_code=403, detail="需要 Pro+ 权限")
    days = override_days if override_days else get_minute_sync_days()
    extend_backward = bool(extend_flag) or days >= 365
    # 默认范围在任务提交时固定, 排队期间修改设置不改变本次实际拉取范围。
    context_key = f"minute_batch:{days}:{extend_backward}"
    context = collection_context(context_key, repo)
    # 分钟K全市场同步是长任务(数据量是日K的 ~240 倍),用更宽松的卡死阈值
    try:
        job_id, is_new = job_store.create(long_running=True, request_key=f"minute_batch:{override_days}:{bool(extend_flag)}")
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not is_new:
        return {"status": "reused", "job_id": job_id}

    async def task() -> None:
        if not try_acquire_run_slot(job_id):
            job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
            return
        loop = asyncio.get_event_loop()

        def progress(stage: str, pct: int, msg: str) -> None:
            job_store.progress(job_id, stage, pct, msg)

        wrote_rows = False

        def on_persist(rows: int) -> None:
            nonlocal wrote_rows
            wrote_rows = wrote_rows or rows > 0

        try:
            progress("sync_minute", 5, "解析标的池…")
            universe = sorted(set(get_pool("watchlist")) | set(get_pool("CN_Equity_A")))
            # 补充 instruments 全量标的，覆盖北交所、新股等
            inst_path = repo.store.data_dir / "instruments" / "instruments.parquet"
            if inst_path.exists():
                with suppress(Exception):
                    import polars as pl
                    inst = pl.read_parquet(inst_path, columns=["symbol"])
                    universe = sorted(set(universe) | set(inst["symbol"].to_list()))
            # 全市场股票任务排除指数，指数由普通分钟采集写入独立分区。
            index_set = repo.get_index_symbol_set()
            universe = [s for s in universe if s not in index_set]
            progress("sync_minute", 10, f"标的池 {len(universe)} 只")

            def _on_chunk(done: int, total: int, seg_label: str) -> None:
                # 进度映射: 10% (标的池解析完) → 95%, 留 5% 给写入+刷新
                pct = 10 + int((done / max(total, 1)) * 85)
                progress("sync_minute", pct, f"拉取分钟K… {done}/{total} 批 [{seg_label}]")

            def _run():
                nonlocal wrote_rows
                from app.services.ordinary_minute import MINUTE_COLLECTION_LOCK
                try:
                    with MINUTE_COLLECTION_LOCK:
                        progress("sync_minute", 10, "开始采集分钟K…")
                        written = kline_sync.sync_and_persist_minute(
                            universe, repo, capset, days=days,
                            extend_backward=extend_backward,
                            on_chunk_done=_on_chunk,
                            on_persist=on_persist,
                        )
                        wrote_rows = wrote_rows or written > 0
                        return written
                finally:
                    if wrote_rows:
                        # 分块已落盘即对读者发布，失败/取消不撤回已完成的块。
                        from app.jobs.daily_pipeline import _refresh_single_view
                        from app.services.shared_runtime import publish_data_update
                        _refresh_single_view(repo, "kline_minute")
                        publish_data_update(repo.store.data_dir, ["minute"])
                        invalidate_storage_cache()

            written = await loop.run_in_executor(_long_task_executor, run_with_capacity, job_id, _run)
            progress("done", 100, f"分钟 K 同步完成,{written} 行")
            result = {"minute_rows": written, "universe_size": len(universe)}
            if context == collection_context(context_key, repo):
                result["collection_context"] = context
            job_store.succeed(job_id, result)
            invalidate_storage_cache()
        except JobCancelledError:
            # 已由 terminate() 标记失败, 拉取线程在分块回调处自行退出
            invalidate_storage_cache()
        except Exception as e:  # noqa: BLE001
            job_store.fail(job_id, str(e))
            invalidate_storage_cache()
        finally:
            release_run_slot(job_id)

    running = asyncio.create_task(task())
    _tasks.add(running)
    running.add_done_callback(_tasks.discard)
    return {"status": "started", "job_id": job_id}


@router.post("/sync_minute_single")
async def sync_minute_single(request: Request, body: dict):
    """手动拉取单只股票的分钟K并落库 (口径随基准标记: 未迁移=前复权, 已迁移=原始)。

    body: { "symbol": "000001.SZ", "days": 5 }，days 为交易日数，范围 1 到 30。
    数据页维护入口按本地交易日历选择最近 days 个交易日并持久化；
    不传 days 则使用分钟同步偏好中的数值，但按本入口的交易日单位解释。
    """
    from app.services.preferences import get_minute_sync_days

    symbol = body.get("symbol", "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol 不能为空")

    requested_days = body.get("days")
    if requested_days is not None:
        if isinstance(requested_days, bool) or not isinstance(requested_days, int):
            raise HTTPException(status_code=400, detail="days 必须是整数")
        if requested_days < 1 or requested_days > 30:
            raise HTTPException(status_code=400, detail="days 必须在 1 到 30 之间")

    repo = request.app.state.repo
    capset = request.app.state.capabilities

    if not _minute_allowed(capset):
        raise HTTPException(status_code=403, detail="分钟数据能力不可用")

    from app.api.pipeline import _submit_job
    from app.services.ordinary_minute import sync_symbol_minutes
    days = requested_days if requested_days is not None else get_minute_sync_days()
    result = await _submit_job(
        repo, getattr(request.app.state, "quote_service", None),
        lambda progress: lambda: sync_symbol_minutes(repo, capset, symbol, days, progress),
        request_key=f"minute:{symbol}:{days}", datasets=["minute"],
    )
    return {"status": "started", "symbol": symbol, **result}


@router.post("/minute-migrate")
async def minute_migrate(request: Request):
    """存量分钟K迁移为原始口径并启用读取时复权 (幂等, 见 services/minute_adjust)。

    - 用日K原始收盘价做锚点, 把历史"拉取时前复权"的分区换算回原始价;
    已是原始价的行 (全量分钟落盘) 锚点 k≈1 自动跳过;
    - 全部分区成功后创建 .raw_basis 标记: 此后读取自动复权投影、拉取改取原始价;
    - 失败不标记, 可重复调用续跑 (已换算分区二次运行为 no-op)。
    返回 { partitions, converted_symbols, skipped_no_daily, failed, marked }。
    """
    import asyncio

    repo = request.app.state.repo
    loop = asyncio.get_event_loop()
    stats = await loop.run_in_executor(
        _long_task_executor,
        lambda: minute_adjust.migrate_minute_to_raw(
            repo.store.data_dir, write_lock=repo._write_lock,
        ),
    )
    if stats.get("marked"):
        from app.jobs.daily_pipeline import _refresh_single_view
        _refresh_single_view(repo, "kline_minute")
        _refresh_single_view(repo, "kline_etf_minute")
    return {"status": "ok", **stats}


@router.post("/clear_minute")
async def clear_minute(request: Request):
    """清空全部分钟K数据 (仅 kline_minute, 不影响其他数据)。

    删除 data/kline_minute/ 下所有分区 parquet, 刷新视图。
    需二次确认: body { "confirm": true }。
    """
    import shutil

    body = await request.json() if request.method == "POST" else {}
    if not body.get("confirm"):
        raise HTTPException(status_code=400, detail="需传 confirm: true 以确认清空")

    repo = request.app.state.repo
    minute_dir = repo.store.data_dir / "kline_minute"

    # 统计待删除行数 (用于返回)
    removed = 0
    if minute_dir.exists():
        # 消费并关闭查询句柄，避免 Windows 文件锁阻止随后删除。
        result = repo.execute_one("SELECT COUNT(*) AS cnt FROM kline_minute")
        removed = result[0] if result else 0
        try:
            with repo._write_lock:
                shutil.rmtree(minute_dir)
        except OSError as exc:
            logger.error("清空分钟K失败: %s", exc)
            raise HTTPException(status_code=500, detail=f"清空分钟K失败: {exc}") from exc
        finally:
            # 删除可能已完成部分分区，读者也须刷新实际剩余文件。
            from app.api.data import invalidate_storage_cache
            from app.jobs.daily_pipeline import _refresh_single_view
            from app.services.shared_runtime import publish_data_update
            _refresh_single_view(repo, "kline_minute")
            invalidate_storage_cache()
            publish_data_update(repo.store.data_dir, ["minute"])

    logger.info("minute K cleared: %d rows removed", removed)
    return {"status": "ok", "removed": removed}


@router.post("/extend_history")
async def extend_history(request: Request):
    """向前扩展历史日K数据 — 独立于盘后管道。

    body: { "value": int, "unit": "day"|"month"|"year" }
    返回 job_id,可轮询 /api/pipeline/jobs 查看进度。
    """
    import asyncio
    import traceback as _tb
    try:
        body = await request.json()
        value = body.get("value")
        unit = body.get("unit", "month")
        if not value or value <= 0:
            raise HTTPException(status_code=400, detail="value 必须为正整数")
        if unit not in ("day", "month", "year"):
            raise HTTPException(status_code=400, detail="unit 只支持 day/month/year")

        repo = request.app.state.repo
        capset = request.app.state.capabilities

        from app.tickflow.capabilities import Cap
        if not capset.has(Cap.KLINE_DAILY_BATCH):
            raise HTTPException(status_code=403, detail="需要 Pro+ 权限 (batch K-line)")

        from app.api.data import invalidate_storage_cache
        from app.services.extend_history import run_extend_history
        from app.services.pipeline_jobs import (
            JobCancelledError,
            job_store,
            release_run_slot,
            run_with_capacity,
            try_acquire_run_slot,
        )

        job_id, is_new = job_store.create(request_key=f"daily_extend:{value}:{unit}")
        if not is_new:
            return {"status": "reused", "job_id": job_id}

        async def task() -> None:
            if not try_acquire_run_slot(job_id):
                job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
                return
            loop = asyncio.get_event_loop()

            def progress(stage: str, pct: int, msg: str,
                         stage_pct: int | None = None, skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg,
                                   stage_pct=stage_pct, skip_log=skip_log)

            try:
                result = await loop.run_in_executor(
                    _long_task_executor, run_with_capacity, job_id,
                    lambda: run_extend_history(repo, capset, value, unit, on_progress=progress),
                )
                if "error" in result:
                    job_store.fail(job_id, result["error"])
                else:
                    job_store.succeed(job_id, result)
                invalidate_storage_cache()
            except JobCancelledError:
                # 已由 terminate() 标记失败, 拉取线程在分块回调处自行退出
                invalidate_storage_cache()
            except Exception as e:
                logger.exception("extend_history failed: job_id=%s", job_id)
                job_store.fail(job_id, str(e))
                invalidate_storage_cache()
            finally:
                release_run_slot(job_id)

        running = asyncio.create_task(task())
        _tasks.add(running)
        running.add_done_callback(_tasks.discard)
        return {"status": "started", "job_id": job_id}
    except HTTPException:
        raise
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as e:
        logger.error("extend_history error: %s\n%s", e, _tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/repair_daily")
async def repair_daily(request: Request):
    """修正 / 补全日K数据 — 从指定起始日期重拉到今天。

    典型场景: 昨天没看盘 / 服务挂了,本地日K缺了若干天。
    用户选起始日期,复用盘后管道全流程重拉 [start_date ~ 今天]。

    body: { "start_date": "YYYY-MM-DD" }
    返回 job_id,可轮询 /api/pipeline/jobs 查看进度。
    """
    import asyncio
    import traceback as _tb
    from datetime import date as _date
    try:
        body = await request.json()
        raw = body.get("start_date")
        if not raw:
            raise HTTPException(status_code=400, detail="start_date 必填 (YYYY-MM-DD)")
        try:
            start_date = _date.fromisoformat(str(raw))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="start_date 格式错误 (应为 YYYY-MM-DD)") from exc

        if start_date > cn_today():
            raise HTTPException(status_code=400, detail="起始日期不能晚于今天")

        repo = request.app.state.repo
        capset = request.app.state.capabilities

        from app.tickflow.capabilities import Cap
        if not capset.has(Cap.KLINE_DAILY_BATCH):
            raise HTTPException(status_code=403, detail="需要 Pro+ 权限 (batch K-line)")

        from app.api.data import invalidate_storage_cache
        from app.services.pipeline_jobs import (
            JobCancelledError,
            job_store,
            release_run_slot,
            run_with_capacity,
            try_acquire_run_slot,
        )
        from app.services.repair_daily import run_repair_daily

        job_id, is_new = job_store.create(request_key=f"daily_repair:{start_date.isoformat()}")
        if not is_new:
            return {"status": "reused", "job_id": job_id}

        async def task() -> None:
            if not try_acquire_run_slot(job_id):
                job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
                return
            loop = asyncio.get_event_loop()
            qs = getattr(request.app.state, "quote_service", None)

            def progress(stage: str, pct: int, msg: str,
                         stage_pct: int | None = None, skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg,
                                   stage_pct=stage_pct, skip_log=skip_log)

            def _run() -> dict:
                # 修正运行期间暂停实时行情, 防止覆写同一批 parquet 竞态
                if qs:
                    with qs.paused():
                        return run_repair_daily(repo, capset, start_date, on_progress=progress)
                return run_repair_daily(repo, capset, start_date, on_progress=progress)

            try:
                result = await loop.run_in_executor(_long_task_executor, run_with_capacity, job_id, _run)
                if "error" in result:
                    job_store.fail(job_id, result["error"])
                else:
                    job_store.succeed(job_id, result)
                invalidate_storage_cache()
            except JobCancelledError:
                # 已由 terminate() 标记失败, 拉取线程在分块回调处自行退出
                invalidate_storage_cache()
            except Exception as e:
                logger.exception("repair_daily failed: job_id=%s", job_id)
                job_store.fail(job_id, str(e))
                invalidate_storage_cache()
            finally:
                release_run_slot(job_id)

        running = asyncio.create_task(task())
        _tasks.add(running)
        running.add_done_callback(_tasks.discard)
        return {"status": "started", "job_id": job_id}
    except HTTPException:
        raise
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as e:
        logger.error("repair_daily error: %s\n%s", e, _tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/rebuild_enriched")
async def rebuild_enriched(request: Request):
    """全量重算 enriched 表 — 不获取任何数据,仅基于已有 kline_daily + adj_factor 重算复权+指标。

    返回 job_id,可轮询 /api/pipeline/jobs 查看进度。
    """
    import asyncio
    try:
        repo = request.app.state.repo

        from app.api.data import invalidate_storage_cache
        from app.services.pipeline_jobs import (
            JobCancelledError,
            job_store,
            release_run_slot,
            run_with_capacity,
            try_acquire_run_slot,
        )

        job_id, is_new = job_store.create(request_key="enriched_rebuild")
        if not is_new:
            return {"status": "reused", "job_id": job_id}

        async def task() -> None:
            if not try_acquire_run_slot(job_id):
                job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
                return
            loop = asyncio.get_event_loop()

            def progress(stage: str, pct: int, msg: str,
                         stage_pct: int | None = None, skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg,
                                   stage_pct=stage_pct, skip_log=skip_log)

            try:
                progress("rebuild_enriched", 10, "全量计算 enriched…")
                from app.indicators.pipeline import run_pipeline

                def _batch_progress(cur: int, tot: int) -> None:
                    pct = 10 + int(85 * cur / tot)
                    progress("rebuild_enriched", pct,
                             f"计算指标 批次 {cur}/{tot}",
                             stage_pct=int(100 * cur / tot), skip_log=True)

                written = await loop.run_in_executor(
                    _long_task_executor, run_with_capacity, job_id,
                    lambda: run_pipeline(on_batch_done=_batch_progress),
                )

                enriched_dir = repo.store.data_dir / "kline_daily_enriched"
                enriched_days = len(list(enriched_dir.glob("date=*"))) if enriched_dir.exists() else 0

                # 刷新视图
                d = repo.store.data_dir.as_posix()
                for view_name, glob in [
                    ("kline_enriched", f"{d}/kline_daily_enriched/**/*.parquet"),
                ]:
                    try:
                        repo.db.execute(
                            f"CREATE OR REPLACE VIEW {view_name} AS "
                            f"SELECT * FROM read_parquet('{glob}', union_by_name=true)"
                        )
                    except Exception:
                        logger.exception("enriched view refresh failed")

                progress("rebuild_enriched", 100, f"完成,覆盖 {enriched_days} 天")
                job_store.succeed(job_id, {
                    "enriched_days": enriched_days,
                    "enriched_rows": written,
                })
                invalidate_storage_cache()
            except JobCancelledError:
                # 已由 terminate() 标记失败, 拉取线程在分块回调处自行退出
                invalidate_storage_cache()
            except Exception as e:
                logger.exception("rebuild_enriched failed: job_id=%s", job_id)
                job_store.fail(job_id, str(e))
                invalidate_storage_cache()
            finally:
                release_run_slot(job_id)

        running = asyncio.create_task(task())
        _tasks.add(running)
        running.add_done_callback(_tasks.discard)
        return {"status": "started", "job_id": job_id}
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as e:
        import traceback as _tb
        logger.error("rebuild_enriched error: %s\n%s", e, _tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e)) from e


# 长时间任务专用线程池（隔离于 FastAPI 默认线程池，防止阻塞请求处理）
_long_task_executor = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="long-task")
