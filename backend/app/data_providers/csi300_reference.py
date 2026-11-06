"""沪深交易所公开统计适配：场内份额及沪深300流通市值（非自由流通市值）。

SSE TOT_VOL、SZSE current_size 均为万份，入口转换为份。
SSE 月报 CPT_NGT 为亿元，入口转换为元；月报未发布的日期保持缺失。
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import date, timedelta

import httpx

FUNDS = ("510300.SH", "510330.SH", "159919.SZ", "510310.SH")
SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_URL = "https://www.szse.cn/api/report/ShowReport/data"


def positive_number(value) -> float:
    result = float(str(value).replace(",", ""))
    if not math.isfinite(result) or result <= 0:
        raise ValueError("公开统计包含无效的份额或市值")
    return result


class Csi300ReferenceSource:
    def __init__(self, check_cancel: Callable[[], None] | None = None):
        self.check = check_cancel or (lambda: None)
        self.client = httpx.Client(
            timeout=httpx.Timeout(20, connect=5),
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.sse.com.cn/"},
            # 公开交易所固定直连；系统代理自动发现会导致本机TLS握手失败。
            trust_env=False,
        )
        self._last_request = 0.0

    def close(self) -> None:
        self.client.close()

    def _get(self, url: str, params: dict):
        self.check()
        delay = 0.5 - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        self.check()
        self._last_request = time.monotonic()
        referer = ("https://fund.szse.cn/marketdata/fundslist/index.html"
                   if url.startswith("https://fund.szse.cn/") else "https://www.szse.cn/market/fund/volume/etf/index.html"
                   if url == SZSE_URL else "https://www.csindex.com.cn/"
                   if "csindex.com.cn" in url else "https://www.sse.com.cn/")
        response = self.client.get(url, params=params, headers={"Referer": referer})
        response.raise_for_status()
        payload = response.json()
        self.check()
        return payload

    def trading_days(self, start: date, end: date) -> list[date]:
        # 走势图接口会在非交易日的查询起点补一个同日锚点（复制下一交易日行情）。
        # 多查一天，再裁回目标窗口，避免将该边界锚点误当作真实交易日。
        query_start = start - timedelta(days=1)
        data = self._get("https://www.csindex.com.cn/csindex-home/perf/index-perf", {
            "indexCode": "000300", "startDate": query_start.strftime("%Y%m%d"),
            "endDate": end.strftime("%Y%m%d"),
        })
        if str(data.get("code")) != "200" or not isinstance(data.get("data"), list):
            raise ValueError("沪深300交易日期查询失败")
        days = []
        for row in data["data"]:
            if row.get("indexCode") != "000300":
                raise ValueError("交易日期返回了其他指数")
            day = date.fromisoformat(str(row["tradeDate"]))
            if start <= day <= end:
                days.append(day)
        if len(days) != len(set(days)):
            raise ValueError("沪深300交易日期重复")
        return sorted(days)

    def index_float_caps(self, start: date, end: date) -> list[dict]:
        month = start.replace(day=1)
        rows = []
        while month <= end:
            payload = self._get(SSE_URL, {
                "sqlId": "COMMON_SSE_ZQZS_M_CSI300_INDEX_C", "isPagination": "false",
                "MDATE": month.strftime("%Y%m"),
            })
            data = payload.get("result")
            if not isinstance(data, list):
                raise ValueError("沪深300官方流通市值响应无效")
            for item in data:
                day = date.fromisoformat(str(item["MDATE"]))
                if day.year != month.year or day.month != month.month:
                    raise ValueError("沪深300流通市值月份不匹配")
                if start <= day <= end:
                    rows.append({"date": day.isoformat(),
                                 "float_cap_yuan": positive_number(item["CPT_NGT"]) * 1e8})
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        if len({r["date"] for r in rows}) != len(rows):
            raise ValueError("沪深300流通市值日期重复")
        return sorted(rows, key=lambda r: r["date"])

    def sse_shares(self, day: date, symbols: tuple[str, ...] = FUNDS,
                   *, require_complete: bool = True) -> list[dict]:
        payload = self._get(SSE_URL, {
            "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L", "isPagination": "true",
            "STAT_DATE": day.isoformat(), "pageHelp.pageSize": "10000", "pageHelp.pageNo": "1",
            "pageHelp.beginPage": "1", "pageHelp.cacheSize": "1", "pageHelp.endPage": "1",
        })
        data = payload.get("result")
        total = payload.get("pageHelp", {}).get("total")
        if not isinstance(data, list) or total is None or int(total) != len(data):
            raise ValueError("上交所ETF份额分页不完整")
        wanted = {s[:6] for s in symbols if s.endswith(".SH")}
        rows = []
        for item in data:
            if item.get("SEC_CODE") not in wanted:
                continue
            if item.get("STAT_DATE") != day.isoformat():
                raise ValueError("上交所ETF份额日期不匹配")
            rows.append({"symbol": item["SEC_CODE"] + ".SH", "date": day.isoformat(),
                         "shares": positive_number(item["TOT_VOL"]) * 1e4})
        if len({r["symbol"] for r in rows}) != len(rows):
            raise ValueError("上交所ETF份额代码重复")
        if require_complete and len(rows) != len(wanted):
            raise ValueError("上交所未完整返回目标ETF份额")
        return rows

    def szse_shares(self, start: date, end: date, symbol: str = "159919.SZ") -> list[dict]:
        rows = []
        while start <= end:
            stop = min(start + timedelta(days=169), end)
            page, counts = 1, None
            batch = []
            while True:
                payload = self._get(SZSE_URL, {
                    "SHOWTYPE": "JSON", "CATALOGID": "scsj_fund_jjgm", "TABKEY": "tab1",
                    "jjlb": "ETF", "txtStart": start.isoformat(), "txtEnd": stop.isoformat(),
                    "txtDm": symbol[:6], "PAGENO": str(page),
                })
                if not isinstance(payload, list) or not payload or payload[0].get("error"):
                    raise ValueError("深交所ETF份额查询失败")
                report = payload[0]
                meta = report["metadata"]
                actual = (int(meta["pagecount"]), int(meta["recordcount"]))
                if counts is None:
                    counts = actual
                if actual != counts or actual[0] > 10:
                    raise ValueError("深交所ETF份额分页发生变化")
                for item in report["data"]:
                    day = date.fromisoformat(item["size_date"])
                    if item["fund_code"] != symbol[:6] or not start <= day <= stop:
                        raise ValueError("深交所ETF份额代码或日期不匹配")
                    batch.append({"symbol": symbol, "date": day.isoformat(),
                                  "shares": positive_number(item["current_size"]) * 1e4})
                if page >= counts[0]:
                    break
                page += 1
            if len(batch) != counts[1]:
                raise ValueError("深交所ETF份额分页不完整")
            rows.extend(batch)
            start = stop + timedelta(days=1)
        if len({r["date"] for r in rows}) != len(rows):
            raise ValueError("深交所ETF份额日期重复")
        return rows
