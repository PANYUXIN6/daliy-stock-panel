"""因果推进的水平箱体识别；输入为同一价格基准、已收盘的有效 K 线。

周期参数是结构展示默认值，不表示交易胜率。边界取首次相反侧拐点的价格，
之后始终固定。局部拐点按右侧窗口确认，平价平台只接纳最后一根。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import polars as pl

Period = Literal["daily", "weekly", "monthly"]
Side = Literal["upper", "lower"]
Direction = Literal["up", "down"]


@dataclass(frozen=True)
class PeriodConfig:
    pivot_left: int
    pivot_right: int
    min_bars: int
    medium_bars: int
    long_bars: int
    touch_tolerance_ratio: float = 0.12
    breakout_margin_ratio: float = 0.05
    breakout_closes: int = 2


PERIOD_CONFIG: dict[Period, PeriodConfig] = {
    "daily": PeriodConfig(2, 2, 15, 30, 60),
    "weekly": PeriodConfig(1, 1, 8, 13, 26),
    "monthly": PeriodConfig(1, 1, 6, 12, 24),
}

# 每个新拐点与最近这些相反侧拐点构造种子；不限制已存在箱体的持续长度。
# 种子数随输入线性增长，避免遍历所有历史拐点配对再回放导致立方级计算。
_OPPOSITE_SEEDS = 24


@dataclass(frozen=True)
class _Pivot:
    index: int
    side: Side
    price: float
    confirmed_index: int | None


class _RangePrices:
    """只查询已发生区间的极值；稀疏表使独立访问判定不重复扫描历史。"""

    def __init__(self, prices: list[float]) -> None:
        self.minimums = [prices]
        self.maximums = [prices]
        span = 2
        while span <= len(prices):
            half = span // 2
            previous_min = self.minimums[-1]
            previous_max = self.maximums[-1]
            self.minimums.append([
                min(previous_min[i], previous_min[i + half])
                for i in range(len(prices) - span + 1)
            ])
            self.maximums.append([
                max(previous_max[i], previous_max[i + half])
                for i in range(len(prices) - span + 1)
            ])
            span *= 2

    def minimum(self, start: int, end: int) -> float:
        level = (end - start + 1).bit_length() - 1
        return min(self.minimums[level][start], self.minimums[level][end - (1 << level) + 1])

    def maximum(self, start: int, end: int) -> float:
        level = (end - start + 1).bit_length() - 1
        return max(self.maximums[level][start], self.maximums[level][end - (1 << level) + 1])


@dataclass
class _Box:
    id: str
    start_index: int
    lower: float
    upper: float
    upper_touches: list[_Pivot] = field(default_factory=list)
    lower_touches: list[_Pivot] = field(default_factory=list)
    alternating: int = 0
    last_side: Side | None = None
    last_touch_index: int = -1
    confirmed_index: int | None = None
    end_index: int | None = None
    status: str = "candidate"
    breakout_status: str = "none"
    breakout_direction: Direction | None = None
    breakout_first: int | None = None
    breakout_confirmed: int | None = None
    outside_direction: Direction | None = None
    outside_count: int = 0
    outside_first: int | None = None
    events: list[tuple[str, int, Direction]] = field(default_factory=list)

    @property
    def width(self) -> float:
        return self.upper - self.lower


def _pivots_at(
    index: int,
    available: int,
    highs: list[float],
    lows: list[float],
    config: PeriodConfig,
    *,
    confirmed: bool,
) -> list[_Pivot]:
    if index < config.pivot_left:
        return []
    left = index - config.pivot_left
    right = min(index + config.pivot_right, available)
    high = all(highs[index] >= value for value in highs[left:index]) and all(
        highs[index] > value for value in highs[index + 1:right + 1]
    )
    low = all(lows[index] <= value for value in lows[left:index]) and all(
        lows[index] < value for value in lows[index + 1:right + 1]
    )
    # 一根同时为高低拐点的长影 K 不能提供可核对的先后往返次序。
    if high == low:
        return []
    return [_Pivot(index, "upper" if high else "lower", highs[index] if high else lows[index],
                   index + config.pivot_right if confirmed else None)]


def _touch(
    box: _Box,
    pivot: _Pivot,
    config: PeriodConfig,
    high_ranges: _RangePrices,
    low_ranges: _RangePrices,
) -> None:
    if pivot.index < box.start_index or pivot.index <= box.last_touch_index:
        return
    tolerance = box.width * config.touch_tolerance_ratio
    boundary = box.upper if pivot.side == "upper" else box.lower
    if abs(pivot.price - boundary) > tolerance + 1e-12:
        return
    touches = box.upper_touches if pivot.side == "upper" else box.lower_touches
    if touches:
        start, end = touches[-1].index + 1, pivot.index - 1
        if start > end:
            return
        # 整根 K 离开边界带两倍容差后才结束一次访问；连续贴边的影线不拆次。
        left_boundary = (
            high_ranges.minimum(start, end) <= box.upper - 2 * tolerance
            if pivot.side == "upper"
            else low_ranges.maximum(start, end) >= box.lower + 2 * tolerance
        )
        if not left_boundary:
            return
    touches.append(pivot)
    box.last_touch_index = pivot.index
    if pivot.confirmed_index is not None and pivot.side != box.last_side:
        box.alternating += 1
        box.last_side = pivot.side


def _outside(box: _Box, close: float, config: PeriodConfig) -> Direction | None:
    margin = box.width * config.breakout_margin_ratio
    if close > box.upper + margin:
        return "up"
    if close < box.lower - margin:
        return "down"
    return None


def _advance(box: _Box, index: int, close: float, config: PeriodConfig) -> None:
    direction = _outside(box, close, config)
    if direction == box.outside_direction and direction is not None:
        box.outside_count += 1
    else:
        if box.status == "confirmed" and box.breakout_status == "pending":
            previous = box.breakout_direction
            if previous is not None:
                box.events.append(("returned", index, previous))
            box.breakout_status = "returned"
        box.outside_direction = direction
        box.outside_count = 1 if direction is not None else 0
        box.outside_first = index if direction is not None else None

    if direction is None:
        return
    if box.status == "candidate":
        if box.outside_count >= config.breakout_closes:
            box.status = "invalidated"
        return
    if box.breakout_status != "pending" or box.breakout_direction != direction:
        box.breakout_status = "pending"
        box.breakout_direction = direction
        box.breakout_first = box.outside_first
        box.events.append(("breakout_pending", index, direction))
    if box.outside_count >= config.breakout_closes:
        box.status = "broken"
        box.breakout_status = "confirmed"
        box.breakout_confirmed = index
        box.end_index = index - 1
        box.events.append(("breakout_confirmed", index, direction))


def _confirm(box: _Box, index: int, config: PeriodConfig) -> None:
    if box.status != "candidate" or index - box.start_index + 1 < config.min_bars:
        return
    if box.alternating < 4 or len(box.upper_touches) < 2 or len(box.lower_touches) < 2:
        return
    box.status = "confirmed"
    box.confirmed_index = index
    if box.outside_direction is not None:
        box.breakout_status = "pending"
        box.breakout_direction = box.outside_direction
        box.breakout_first = box.outside_first
        box.events.append(("breakout_pending", index, box.outside_direction))


def _same_range(box: _Box, lower: float, upper: float, config: PeriodConfig) -> bool:
    tolerance = min(box.width, upper - lower) * config.touch_tolerance_ratio
    return abs(box.lower - lower) <= tolerance and abs(box.upper - upper) <= tolerance


def _seed(
    first: _Pivot,
    last: _Pivot,
    index: int,
    history: list[_Pivot],
    active: list[_Box],
    dates: list[str],
    closes: list[float],
    config: PeriodConfig,
    period: Period,
    high_ranges: _RangePrices,
    low_ranges: _RangePrices,
) -> _Box | None:
    upper = first.price if first.side == "upper" else last.price
    lower = first.price if first.side == "lower" else last.price
    if upper <= lower or any(_same_range(box, lower, upper, config) for box in active
                             if box.status in {"candidate", "confirmed"}):
        return None
    tolerance = (upper - lower) * config.touch_tolerance_ratio
    if (high_ranges.maximum(first.index, index) > upper + tolerance
            or low_ranges.minimum(first.index, index) < lower - tolerance):
        return None
    upper_pivot, lower_pivot = (first, last) if first.side == "upper" else (last, first)
    box = _Box(f"{period}:{dates[upper_pivot.index]}:{dates[lower_pivot.index]}",
               first.index, lower, upper)
    # 新种子不能跨越一个按其固定边界已确认的历史突破再复活旧结构。
    for prior in range(first.index, index + 1):
        _advance(box, prior, closes[prior], config)
        if box.status == "invalidated":
            return None
    for pivot in history:
        if pivot.index >= first.index:
            _touch(box, pivot, config, high_ranges, low_ranges)
    if last.confirmed_index is None:
        _touch(box, last, config, high_ranges, low_ranges)
    return box


def _serialize(box: _Box, dates: list[str], config: PeriodConfig, period: Period) -> dict:
    end = box.end_index if box.end_index is not None else len(dates) - 1
    duration = end - box.start_index + 1
    missing = []
    confirmed_upper = sum(touch.confirmed_index is not None for touch in box.upper_touches)
    confirmed_lower = sum(touch.confirmed_index is not None for touch in box.lower_touches)
    if duration < config.min_bars:
        missing.append(f"有效K线不足{config.min_bars}根")
    if confirmed_upper < 2:
        missing.append("上沿不足两次已确认独立试探")
    if confirmed_lower < 2:
        missing.append("下沿不足两次已确认独立试探")
    if box.alternating < 4:
        missing.append("不足四次已确认交替往返")
    if any(touch.confirmed_index is None for touch in [*box.upper_touches, *box.lower_touches]):
        missing.append("最新触点尚待右侧K线确认")

    def touches(values: list[_Pivot]) -> list[dict]:
        return [{"date": dates[touch.index], "price": touch.price,
                 "confirmed_date": dates[touch.confirmed_index]
                 if touch.confirmed_index is not None else None} for touch in values]

    return {
        "id": box.id, "period": period,
        "level": "long" if duration >= config.long_bars else "medium"
        if duration >= config.medium_bars else "short",
        "lower": box.lower, "upper": box.upper, "width": box.width,
        "width_pct": box.width / box.lower * 100,
        "start_date": dates[box.start_index], "end_date": dates[end],
        "observed_until": dates[-1],
        "confirmed_date": dates[box.confirmed_index] if box.confirmed_index is not None else None,
        "duration_bars": duration, "status": box.status,
        "upper_touches": touches(box.upper_touches), "lower_touches": touches(box.lower_touches),
        "missing_conditions": missing if box.status == "candidate" else [], "parent_id": None,
        "breakout": {"status": box.breakout_status, "direction": box.breakout_direction,
                     "first_date": dates[box.breakout_first] if box.breakout_first is not None else None,
                     "confirmed_date": dates[box.breakout_confirmed]
                     if box.breakout_confirmed is not None else None},
        "events": [{"type": kind, "date": dates[index], "direction": direction}
                   for kind, index, direction in box.events],
    }


def compute_box_oscillation(bars: pl.DataFrame, period: Period = "daily") -> list[dict]:
    """识别一段连续有效、已收盘的前复权 OHLC；不补点、不采集、不写缓存。

    每根 K 只在当时右侧窗口就绪后接纳拐点；确认时间不会回填到形态起点。
    每个拐点最多构造 24 个种子，总推进和重复范围合并至多二次复杂度。
    输入缺口由调用方分段；未完成周/月 K 也由调用方剔除。
    """
    if period not in PERIOD_CONFIG:
        raise ValueError("不支持的箱体K线周期")
    if bars.is_empty():
        return []
    required = {"date", "open", "high", "low", "close"}
    if not required <= set(bars.columns):
        raise ValueError("箱体识别缺少OHLC字段")
    frame = bars.sort("date")
    dates = [value.isoformat()[:10] if hasattr(value, "isoformat") else str(value)[:10]
             for value in frame["date"].to_list()]
    opens, highs, lows, closes = ([float(value) for value in frame[column].to_list()]
                                for column in ("open", "high", "low", "close"))
    if any(not all(math.isfinite(value) and value > 0 for value in prices)
           or prices[2] > min(prices[0], prices[3])
           or prices[1] < max(prices[0], prices[3])
           or prices[1] < prices[2]
           for prices in zip(opens, highs, lows, closes, strict=True)):
        raise ValueError("箱体识别需要有效的正数OHLC")
    config = PERIOD_CONFIG[period]
    high_ranges, low_ranges = _RangePrices(highs), _RangePrices(lows)
    history: list[_Pivot] = []
    active: list[_Box] = []
    completed: list[_Box] = []

    for index, close in enumerate(closes):
        for box in active:
            _advance(box, index, close, config)
        pivots = _pivots_at(index - config.pivot_right, index, highs, lows, config, confirmed=True)
        for pivot in pivots:
            for box in active:
                if box.status in {"candidate", "confirmed"}:
                    _touch(box, pivot, config, high_ranges, low_ranges)
            opposite = [item for item in history if item.side != pivot.side][-_OPPOSITE_SEEDS:]
            history.append(pivot)
            for first in opposite:
                box = _seed(first, pivot, index, history, active, dates, closes, config,
                            period, high_ranges, low_ranges)
                if box is not None:
                    active.append(box)
        retained = []
        for box in active:
            _confirm(box, index, config)
            if box.status == "broken":
                completed.append(box)
            elif box.status != "invalidated":
                retained.append(box)
        active = retained

    # 尾端未确认拐点只为候选提供证据，不影响已形成箱体及历史事件。
    for index in range(max(config.pivot_left, len(dates) - config.pivot_right), len(dates)):
        for pivot in _pivots_at(index, len(dates) - 1, highs, lows, config, confirmed=False):
            for box in active:
                if box.status == "candidate":
                    _touch(box, pivot, config, high_ranges, low_ranges)
            opposite = [item for item in history if item.side != pivot.side][-_OPPOSITE_SEEDS:]
            for first in opposite:
                box = _seed(first, pivot, len(dates) - 1, history, active, dates, closes,
                            config, period, high_ranges, low_ranges)
                if box is not None:
                    active.append(box)

    result = [_serialize(box, dates, config, period) for box in [*completed, *active]]
    for child in result:
        parents = [parent for parent in result
                   if parent["confirmed_date"] is not None
                   and parent["lower"] <= child["lower"] and child["upper"] <= parent["upper"]
                   and parent["width"] > child["width"]
                   and parent["start_date"] <= child["start_date"]
                   and child["end_date"] <= parent["end_date"]]
        if parents:
            child["parent_id"] = min(parents, key=lambda parent: parent["width"])["id"]
    return sorted(result, key=lambda box: (box["start_date"], -box["width"], box["id"]))
