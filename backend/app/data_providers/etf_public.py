"""交易所公开ETF目录。份额复用交易所适配器，单位为份。

沪市按官方跟踪指数精确匹配；深市按官方扩位简称的完整指数前缀匹配。
不将包含300/500等字样的行业、策略、增强基金归入对应宽基。
"""
from __future__ import annotations

import html
import re

from app.data_providers.csi300_reference import SSE_URL, Csi300ReferenceSource

INDEX_CATEGORIES = {
    "沪深300指数": "沪深300", "中证1000指数": "中证1000",
    "中证500指数": "中证500", "中证小盘500指数": "中证500",
    "上证科创50成份指数": "科创50", "上证科创板50成份指数": "科创50", "中证A500指数": "中证A500",
    "创业板指数": "创业板指", "创业板指": "创业板指", "上证50指数": "上证50",
    "创业板50指数": "创业板50", "中证科创创业50指数": "双创50",
    "中证2000指数": "中证2000", "国证2000指数": "国证2000",
    "上证综合指数": "上证综指",
}
NAME_CATEGORIES = {
    "沪深300": "沪深300", "中证1000": "中证1000", "中证500": "中证500",
    "科创50": "科创50", "中证A500": "中证A500", "A500": "中证A500",
    "创业板": "创业板指", "创业板指": "创业板指", "创业板指数": "创业板指",
    "上证50": "上证50", "创业板50": "创业板50", "双创50": "双创50",
    "科创创业50": "双创50", "中证2000": "中证2000", "国证2000": "国证2000",
    "上证指数": "上证综指", "上证综指": "上证综指",
}


def plain_text(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", value)).strip()


def category_from_name(name: str) -> str | None:
    if any(word in name for word in ("增强", "联接", "红利", "低波", "ESG")):
        return None
    prefix, sep, _ = name.partition("ETF")
    return NAME_CATEGORIES.get(prefix) if sep else None


class EtfPublicSource(Csi300ReferenceSource):
    def catalog(self) -> list[dict]:
        data = self._get(SSE_URL, {
            "sqlId": "COMMON_JJZWZ_JJLB_L", "isPagination": "true",
            "pageHelp.pageSize": "10000", "pageHelp.pageNo": "1",
        })
        records = data.get("result")
        if not isinstance(records, list) or len(records) != int(data.get("pageHelp", {}).get("total", -1)):
            raise ValueError("上交所基金目录分页不完整")
        rows = []
        for item in records:
            category = INDEX_CATEGORIES.get(item["INDEX_NAME"])
            name = item.get("FUND_EXPANSION_ABBR") or item["FUND_ABBR"]
            if category and "ETF" in name and not any(s in name for s in ("增强", "联接")):
                rows.append({"symbol": item["FUND_CODE"] + ".SH", "name": name,
                             "category": category, "list_date": item["LISTING_DATE"],
                             "classification": "交易所跟踪指数"})
        page, counts, received = 1, None, 0
        while True:
            payload = self._get("https://fund.szse.cn/api/report/ShowReport/data", {
                "SHOWTYPE": "JSON", "CATALOGID": "1000_lf", "TABKEY": "tab1", "PAGENO": str(page),
            })
            if not isinstance(payload, list) or not payload or payload[0].get("error"):
                raise ValueError("深交所基金目录不可用")
            report = payload[0]
            meta = report["metadata"]
            actual = (int(meta["pagecount"]), int(meta["recordcount"]))
            counts = counts or actual
            if counts != actual or actual[0] > 200:
                raise ValueError("深交所基金目录分页发生变化")
            received += len(report["data"])
            for item in report["data"]:
                name = plain_text(item["jjjcurl"])
                category = category_from_name(name)
                if item["jjlb"] != "ETF" or category is None:
                    continue
                code = plain_text(item["sys_key"])
                if not re.fullmatch(r"\d{6}", code):
                    raise ValueError("深交所基金代码无效")
                rows.append({"symbol": code + ".SZ", "name": name, "category": category,
                             "list_date": item["ssrq"], "classification": "交易所扩位简称"})
            if page >= actual[0]:
                break
            page += 1
        if received != counts[1] or len(rows) != len({r["symbol"] for r in rows}):
            raise ValueError("交易所基金目录缺失或重复")
        return sorted(rows, key=lambda row: row["symbol"])
