"""盘后缺行的独立状态证据：东方财富停复牌及 IPO 日程，不提供或替换行情。

接口字段依据 AKShare 官方 stock_tfp_em / stock_xgsglb_em 和实际响应核对。
每轮重新请求，仅明确覆盖目标交易日的状态可以解释缺行，不推测未知状态。
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time

import httpx

from app.market_time import CN_TZ, cn_today

_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_SYMBOL = re.compile(r"\d{6}\.(?:SH|SZ|BJ)\Z")
_MAX_PAGES = 100


class TradingStatusError(RuntimeError):
    """状态数据不可用或响应不完整，不能据此豁免日线缺失。"""


@dataclass(frozen=True)
class DailyTradingStatus:
    day: date
    source: str = "eastmoney"
    halted: frozenset[str] = field(default_factory=frozenset)
    not_listed: frozenset[str] = field(default_factory=frozenset)


def _date(value) -> date | None:
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    # 上游有时把“上市日期未知”编码为 Unix 零点，不能当成已上市日期。
    return parsed if parsed >= date(1990, 1, 1) else None


def _datetime(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(CN_TZ).replace(tzinfo=None)
    return parsed


def _halted_on(row: dict, day: date) -> bool:
    if not {"SUSPEND_START_TIME", "SUSPEND_END_TIME", "SUSPEND_EXPIRE", "PREDICT_RESUME_DATE"}.issubset(row):
        return False
    if row.get("SUSPEND_EXPIRE") not in {"连续停牌", "停牌一天", "全天停牌"}:
        return False
    start = _datetime(row.get("SUSPEND_START_TIME"))
    if start is None or start > datetime.combine(day, time(9, 30)):
        return False
    end_value = row.get("SUSPEND_END_TIME")
    end = _datetime(end_value)
    if end_value is not None and end is None:
        return False
    if end is not None and end < datetime.combine(day, time(15)):
        return False
    # “停牌一天”不能因缺少终点而被无限延长。
    if end is None and row.get("SUSPEND_EXPIRE") != "连续停牌" and start.date() != day:
        return False
    resume_value = row.get("PREDICT_RESUME_DATE")
    resume = _date(resume_value)
    if resume_value is not None and resume is None:
        return False
    return resume is None or day < resume


def _not_listed_on(row: dict, day: date, observed_day: date) -> bool:
    listed = _date(row.get("LISTING_DATE")) or _date(row.get("SELECT_LISTING_DATE"))
    if listed is not None:
        return day < listed
    # 空上市日期本身不作证据；只有发行日程明确返回“待上市”才可解释缺行。
    # 这是查询时点的状态，因此不能用于尚未到来的交易日。
    return (day <= observed_day and row.get("CONTINUOUS_1WORD_NUM") == "待上市"
            and _date(row.get("APPLY_DATE")) is not None)


def _read_report(client: httpx.Client, report: str, query: str, *,
                 check_cancel: Callable[[], None]) -> list[dict]:
    params = {
        "reportName": report, "columns": "ALL", "filter": query,
        "pageSize": 500, "pageNumber": 1, "source": "WEB", "client": "WEB",
        "sortColumns": "SUSPEND_START_DATE" if report == "RPT_CUSTOM_SUSPEND_DATA_INTERFACE" else "APPLY_DATE,SECURITY_CODE",
        "sortTypes": "-1" if report == "RPT_CUSTOM_SUSPEND_DATA_INTERFACE" else "-1,-1",
    }
    rows: list[dict] = []
    pages = count = None
    page = 1
    while pages is None or page <= pages:
        check_cancel()
        params["pageNumber"] = page
        response = client.get(_URL, params=params)
        check_cancel()
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TradingStatusError("交易状态响应格式无效")
        # 供应商用此业务码明确表达查询无记录；它不会给任何标的产生豁免。
        if payload.get("code") == 9201 and payload.get("result") is None and page == 1:
            return []
        result = payload.get("result")
        if (payload.get("success") is not True or payload.get("code") != 0
                or not isinstance(result, dict)):
            raise TradingStatusError("交易状态查询未成功")
        current_pages, current_count = result.get("pages"), result.get("count")
        data = result.get("data")
        if (type(current_pages) is not int or not 0 <= current_pages <= _MAX_PAGES
                or type(current_count) is not int or current_count < 0
                or (current_pages == 0 and current_count != 0)
                or not isinstance(data, list) or any(not isinstance(row, dict) for row in data)):
            raise TradingStatusError("交易状态分页格式无效")
        if pages is None:
            pages, count = current_pages, current_count
        if (current_pages, current_count) != (pages, count):
            raise TradingStatusError("交易状态分页在查询期间发生变化")
        rows.extend(data)
        if count and not data:
            raise TradingStatusError("交易状态分页不完整")
        page += 1
    if len(rows) != count:
        raise TradingStatusError("交易状态记录数量不完整")
    return rows


def _ipo_batches(symbols: set[str]) -> Iterator[tuple[str, str]]:
    for exchange in ("SH", "SZ", "BJ"):
        codes = sorted(s.split(".")[0] for s in symbols if s.endswith(f".{exchange}"))
        for offset in range(0, len(codes), 100):
            items = ",".join(f'"{code}"' for code in codes[offset:offset + 100])
            report = "RPT_NEEQ_ISSUEINFO_LIST" if exchange == "BJ" else "RPTA_APP_IPOAPPLY"
            yield report, f"(SECURITY_CODE in ({items}))"


def get_daily_trading_status(day: date, symbols: list[str], *,
                             check_cancel: Callable[[], None] | None = None) -> DailyTradingStatus:
    """仅检查候选缺行，返回已确认的全天停牌和尚未上市状态。

    停牌结束时刻包含当日 15:00；复牌日和上市日当天仍须正式行情。
    失败不返回部分证据，不缓存进程或磁盘结果，下次任务重新核对。
    """
    check = check_cancel or (lambda: None)
    check()
    observed_day = cn_today()
    if day > observed_day:
        raise TradingStatusError("不能使用当前交易状态核验未来交易日")
    requested = {s for s in symbols if isinstance(s, str) and _SYMBOL.fullmatch(s)}
    if not requested:
        return DailyTradingStatus(day)
    halted: set[str] = set()
    not_listed: set[str] = set()
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            rows = _read_report(client, "RPT_CUSTOM_SUSPEND_DATA_INTERFACE",
                                f'(MARKET="全部")(DATETIME=\'{day.isoformat()}\')',
                                check_cancel=check)
            for row in rows:
                symbol = row.get("SECUCODE")
                if symbol in requested and _halted_on(row, day):
                    halted.add(symbol)
            remaining = requested - halted
            # 以调用方的完整代码映射为准，不根据裸代码前缀猜测交易所或资产类型。
            code_map = {s.split(".")[0]: s for s in remaining}
            ambiguous = {code for code, count in Counter(s.split(".")[0] for s in remaining).items()
                         if count > 1}
            for report, query in _ipo_batches(remaining):
                for row in _read_report(client, report, query, check_cancel=check):
                    code = row.get("SECURITY_CODE")
                    symbol = code_map.get(code)
                    market = {"上海证券交易所": "SH", "深圳证券交易所": "SZ", "北京证券交易所": "BJ"}.get(row.get("TRADE_MARKET"))
                    matches = (symbol and (row.get("SECUCODE") == symbol
                                           or (market and symbol.endswith(f".{market}"))))
                    if code not in ambiguous and matches and _not_listed_on(row, day, observed_day):
                        not_listed.add(symbol)
    except (httpx.HTTPError, ValueError, ImportError) as exc:
        raise TradingStatusError("停复牌／上市状态数据获取失败") from exc
    check()
    return DailyTradingStatus(day, halted=frozenset(halted), not_listed=frozenset(not_listed))
