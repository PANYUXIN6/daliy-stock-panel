"""正式财报满足利润条件后，完整向上跳空形成当天选股。"""

from app.strategy.market_data import get_earnings_gap_signals

META = {
    "id": "earnings_breakout_gap",
    "name": "业绩断层跳空",
    "description": (
        "筛选所选交易日首次形成业绩断层的股票，对应K线“断”标记。"
        "仅使用当时可用的同源正式利润表版本：净利润为正且同比增长，"
        "在该财报首次可用日后的首个交易日，最低价高于前一交易K线最高价。"
        "不要求回踩，不叠加PSY、多空线或阳线条件；缺少历史财务依据不补信号。"
        "手动筛选最新交易日时先找完整向上缺口，仅更新候选股扶摇利润表，"
        "页面仅展示已确认的业绩断层跳空，证据不足或条件未通过不展示。"
        "支持手动日线筛选，盘中缺口以收盘确认为准，不启用实时监控。"
    ),
    "tags": ["业绩", "跳空", "断层"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "realtime_supported": False,
    "params": [],
    "scoring": {},
    "order_by": "symbol",
    "descending": False,
    "limit": 0,
}

BASIC_FILTER = {"enabled": False}
LOOKBACK_DAYS = 2
REQUIRED_FEATURES = {"open", "high", "low", "close", "volume", "amount"}


def filter_history(df, params):
    del params
    return get_earnings_gap_signals(df)
