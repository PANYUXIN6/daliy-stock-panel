"""巨潮沪深股票公告与东方财富转载的交易公开信息。只读取披露日、上榜交易日及披露原因。"""
from __future__ import annotations

import re
from datetime import date, datetime

import httpx

from app.market_time import CN_TZ

CNINFO = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
BILLBOARD = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _disclosures(client, first: str, last: str):
    """股票严重异常波动公告的官方披露日期。巨潮空查询返回 announcements=null。"""
    records = []
    page = 1
    while True:
        response = client.post(CNINFO, data={
            "column": "szse", "tabName": "fulltext", "searchkey": "严重异常波动",
            "pageNum": str(page), "pageSize": "30", "seDate": f"{first}~{last}",
        }, headers={"Referer": "https://www.cninfo.com.cn/", "User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        payload = response.json()
        total = payload["totalAnnouncement"]
        if not isinstance(total, int) or total < 0:
            raise ValueError("严重异动公告数量无效")
        rows = payload["announcements"]
        if rows is None and total == 0:
            rows = []
        if not isinstance(rows, list):
            raise ValueError("严重异动公告查询未完成")
        records.extend(rows)
        if len(records) == total and payload["hasMore"] is False:
            break
        if not rows or not payload["hasMore"] or len(records) >= total:
            raise ValueError("严重异动公告分页缺失")
        page += 1
    result = []
    for row in records:
        title = re.sub(r"<[^>]+>", "", row["announcementTitle"])
        code = row["secCode"]
        if not re.fullmatch(r"(?:60\d{4}|00\d{4}|30\d{4})", code):
            continue
        # 跟踪异常事实首次披露。澄清、纠错、进展和复牌核查结果不是新的发生公告。
        subject = r"(?:股票(?:交易|价格)?|股价)(?:异常波动[暨、及和与])?(?:出现|发生|触及)?严重异常波动"
        if (not re.search(subject, title) or not title.endswith("公告")
                or re.search(r"未触及|未发生|可能|预计|澄清|更正|补充|进展|结果|复牌|可转债|债券|回函|复函", title)):
            continue
        published = datetime.fromtimestamp(row["announcementTime"] / 1000, CN_TZ).date().isoformat()
        if not first <= published <= last:
            raise ValueError("公告披露日期超出查询区间")
        symbol = code + (".SH" if code.startswith("6") else ".SZ")
        result.append({"symbol": symbol, "date": published, "id": row["announcementId"], "title": title})
    return result


def _ordinary_moves(client, first: str, last: str):
    """去除一日偏离、换手率榜及其他非三日价格异常原因，证券按公开代码识别。"""
    params = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": "SECUCODE,TRADE_DATE,EXPLANATION",
        "filter": f"(TRADE_DATE>='{first}')(TRADE_DATE<='{last}')", "pageSize": 500, "pageNumber": 1,
        "sortColumns": "TRADE_DATE,SECUCODE,EXPLANATION", "sortTypes": "1,1,1", "source": "WEB", "client": "WEB",
    }
    moves = set()
    received = 0
    while True:
        response = client.get(BILLBOARD, params=params)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") == 9201 and payload.get("result") is None and received == 0:
            return moves
        if payload.get("code") != 0 or payload.get("success") is not True:
            raise ValueError("交易公开信息查询失败")
        batch = payload["result"]
        for row in batch["data"]:
            symbol, reason = row["SECUCODE"], row["EXPLANATION"]
            if not re.fullmatch(r"(?:60\d{4}\.SH|(?:00|30)\d{4}\.SZ)", symbol):
                continue
            threshold = "30%" if symbol.startswith("30") else "20%"
            if not (re.search(r"连续[3三]个交易日内", reason) and "偏离值累计达到" + threshold in reason):
                continue
            for direction in ("涨", "跌"):
                if direction + "幅偏离值" in reason:
                    moves.add((symbol, date.fromisoformat(row["TRADE_DATE"][:10]).isoformat(), direction))
        received += len(batch["data"])
        if params["pageNumber"] >= batch["pages"]:
            if received != batch["count"]:
                raise ValueError("交易公开信息分页缺失")
            return moves
        if not batch["data"]:
            raise ValueError("交易公开信息分页缺失")
        params["pageNumber"] += 1


def fetch_disclosures(first: str, last: str):
    with httpx.Client(timeout=8) as client:
        return _disclosures(client, first, last), _ordinary_moves(client, first, last)
