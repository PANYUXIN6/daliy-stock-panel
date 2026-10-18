"""复用主图业绩断层计算，筛选当日缺口或中点回踩。"""

from app.strategy.market_data import get_earnings_breakout_signals

META = {
    "id": "earnings_breakout_pullback",
    "name": "业绩断层回踩",
    "description": (
        "筛选所选交易日出现“缺口”或“中点”回踩信号的股票，与K线业绩断层指标共用计算。"
        "仅使用当时可用的正式利润表版本，净利润为正且同比增长后形成向上跳空；"
        "回踩还须满足PSY≤50、收盘在多空线上、线或MA20上行及阳线条件。"
        "缺少历史财务依据不补信号；支持手动日线筛选，盘中结果以收盘确认为准，不启用实时监控。"
    ),
    "tags": ["业绩", "缺口", "回踩"],
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
# 调用方只需提供当期行情；完整前序行情由共享只读入口批量读取，避免截断EMA和缺口锚点。
LOOKBACK_DAYS = 2
REQUIRED_FEATURES = {"open", "high", "low", "close", "volume", "amount"}


def filter_history(df, params):
    del params
    return get_earnings_breakout_signals(df)
