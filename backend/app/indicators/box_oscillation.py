"""因果推进的水平箱体识别；输入为同一价格基准、已收盘的有效 K 线。

周期参数是结构展示默认值，不表示交易胜率。种子固定支撑／压力区域，
候选代表价用独立触点中位数修正，确认后冻结。局部拐点只在右侧窗口完成后使用。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from statistics import median
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
    touch_tolerance_ratio: float = 0.20
    release_distance_ratio: float = 0.24
    dedup_tolerance_ratio: float = 0.20
    fine_pivot_window: int | None = None
    min_width_retention_ratio: float = 0.70
    breakout_margin_ratio: float = 0.15
    breakout_closes: int = 2


PERIOD_CONFIG: dict[Period, PeriodConfig] = {
    "daily": PeriodConfig(2, 2, 8, 30, 60, fine_pivot_window=1),
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
    events: list[tuple[str, int, Direction]] = field(default_factory=list)
    revisits: list[tuple[str, int, Side]] = field(default_factory=list)
    previous_box_id: str | None = None
    previous_close_side: Side | None = None
    revisit_armed: set[Side] = field(default_factory=set)

    anchor_lower: float = field(init=False)
    anchor_upper: float = field(init=False)

    def __post_init__(self) -> None:
        self.anchor_lower = self.lower
        self.anchor_upper = self.upper

    @property
    def anchor_width(self) -> float:
        return self.anchor_upper - self.anchor_lower

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
    tolerance = box.anchor_width * config.touch_tolerance_ratio
    boundary = box.anchor_upper if pivot.side == "upper" else box.anchor_lower
    if abs(pivot.price - boundary) > tolerance + 1e-12:
        return
    touches = box.upper_touches if pivot.side == "upper" else box.lower_touches
    if touches:
        start, end = touches[-1].index + 1, pivot.index - 1
        if start > end:
            return
        # 离开距离独立于触碰容差；扩大接纳区域不会反过来收紧独立访问。
        release = box.anchor_width * config.release_distance_ratio
        left_boundary = (
            high_ranges.minimum(start, end) <= box.anchor_upper - release
            if pivot.side == "upper"
            else low_ranges.maximum(start, end) >= box.anchor_lower + release
        )
        if not left_boundary:
            return
    touches.append(pivot)
    box.last_touch_index = pivot.index
    if pivot.confirmed_index is not None and pivot.side != box.last_side:
        box.alternating += 1
        box.last_side = pivot.side
    if box.status == "candidate" and pivot.confirmed_index is not None:
        # 接纳区域始终锚定种子，不能通过连续小步更新把趋势吞入箱体。
        confirmed_prices = [touch.price for touch in touches if touch.confirmed_index is not None]
        if len(confirmed_prices) >= 2:
            if pivot.side == "upper":
                box.upper = median(confirmed_prices)
            else:
                box.lower = median(confirmed_prices)


def _outside(box: _Box, close: float, config: PeriodConfig) -> Direction | None:
    # 候选尚未冻结代表价，历史失效按固定锚点判断；确认后只用冻结代表价。
    lower, upper = ((box.anchor_lower, box.anchor_upper) if box.confirmed_index is None
                    else (box.lower, box.upper))
    margin = (upper - lower) * config.breakout_margin_ratio
    if close > upper + margin:
        return "up"
    if close < lower - margin:
        return "down"
    return None


def _position(box: _Box, close: float) -> Direction | None:
    return "up" if close > box.upper else "down" if close < box.lower else None


def _advance(box: _Box, index: int, close: float, config: PeriodConfig) -> None:
    direction = _outside(box, close, config)
    if direction == box.outside_direction and direction is not None:
        box.outside_count += 1
    else:
        box.outside_direction = direction
        box.outside_count = 1 if direction is not None else 0

    if box.status == "candidate":
        if box.outside_count >= config.breakout_closes:
            box.status = "invalidated"
        return
    # 越过代表线就提示；回到缓冲以内但仍在箱外，不等于回箱。
    position = _position(box, close)
    if position is None:
        if box.breakout_status == "pending" and box.breakout_direction is not None:
            box.events.append(("returned", index, box.breakout_direction))
            box.breakout_status = "returned"
        return
    if box.breakout_status != "pending" or box.breakout_direction != position:
        box.breakout_status = "pending"
        box.breakout_direction = position
        box.breakout_first = index
        box.events.append(("breakout_pending", index, position))
    if box.outside_count >= config.breakout_closes:
        box.status = "broken"
        box.breakout_status = "confirmed"
        box.breakout_confirmed = index
        box.end_index = index - 1
        box.events.append(("breakout_confirmed", index, direction))


def _advance_history(box: _Box, index: int, high: float, low: float, close: float,
                     config: PeriodConfig, *, record: bool = True) -> None:
    """只记录结束后的回访，不延长原框、累加原触点或改变突破结论。"""
    position = _position(box, close)
    side: Side | None = "upper" if position == "up" else "lower" if position == "down" else None
    tolerance = box.anchor_width * config.touch_tolerance_ratio
    entered = record and side is None and box.previous_close_side is not None
    if entered:
        box.revisits.append(("entered", index, box.previous_close_side))
    for boundary_side, boundary in (("upper", box.anchor_upper), ("lower", box.anchor_lower)):
        if low <= boundary + tolerance and high >= boundary - tolerance:
            if record and not entered and boundary_side in box.revisit_armed:
                box.revisits.append(("touch", index, boundary_side))
            box.revisit_armed.discard(boundary_side)
        else:
            # 两侧独立判断；向箱内或箱外完整离开区域后，才允许下一次回访。
            box.revisit_armed.add(boundary_side)
    box.previous_close_side = side


def _retains_range(box: _Box, config: PeriodConfig) -> bool:
    # 容许边界附近噪声，但两侧同时向内移动后已不再往返原有水平范围。
    upper = [touch.price for touch in box.upper_touches if touch.confirmed_index is not None]
    lower = [touch.price for touch in box.lower_touches if touch.confirmed_index is not None]
    return (not upper or not lower
            or upper[-1] - lower[-1] >= box.anchor_width * config.min_width_retention_ratio)


def _confirm(box: _Box, index: int, config: PeriodConfig, closes: list[float]) -> None:
    if box.status != "candidate" or index - box.start_index + 1 < config.min_bars:
        return
    if box.alternating < 4 or len(box.upper_touches) < 2 or len(box.lower_touches) < 2:
        return
    if not _retains_range(box, config):
        return
    box.status = "confirmed"
    box.confirmed_index = index
    # 从此时起冻结代表边界，并用当时已知收盘重新核对连续越界。
    # 候选锚点与触点中位数可以不同，不能确认一个按最终边界已突破的框。
    direction = _outside(box, closes[index], config)
    count = 0
    if direction is not None:
        for prior in range(index, max(box.start_index - 1, index - config.breakout_closes), -1):
            if _outside(box, closes[prior], config) != direction:
                break
            count += 1
    if count >= config.breakout_closes:
        box.status = "invalidated"
        box.confirmed_index = None
        return
    box.outside_direction = direction
    box.outside_count = count
    position = _position(box, closes[index])
    if position is not None:
        box.breakout_status = "pending"
        box.breakout_direction = position
        box.breakout_first = index
        box.events.append(("breakout_pending", index, position))


def _same_range(box: _Box, lower: float, upper: float, config: PeriodConfig) -> bool:
    tolerance = min(box.width, upper - lower) * config.dedup_tolerance_ratio
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
    upper_pivot, lower_pivot = (first, last) if first.side == "upper" else (last, first)
    box = _Box(f"{period}:{dates[upper_pivot.index]}:{dates[lower_pivot.index]}",
               first.index, lower, upper)
    # 新种子不能跨越一个按其固定边界已确认的历史突破再复活旧结构。
    wick_excursions = 0
    tolerance = box.anchor_width * config.touch_tolerance_ratio
    for prior in range(first.index, index + 1):
        outside_wick = (high_ranges.maximum(prior, prior) > upper + tolerance
                        or low_ranges.minimum(prior, prior) < lower - tolerance)
        if outside_wick and _outside(box, closes[prior], config) is None:
            wick_excursions += 1
            # 只豁免单根已收回的影线；收盘离开则统一交给连续收盘规则。
            if wick_excursions > 1:
                return None
        _advance(box, prior, closes[prior], config)
        if box.status == "invalidated":
            return None
    for pivot in sorted(history, key=lambda item: item.index):
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
    if not _retains_range(box, config):
        missing.append("近期上下沿明显收敛，未维持原水平范围")
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
        "lower_zone": [max(0.0, box.anchor_lower - box.anchor_width * config.touch_tolerance_ratio),
                       box.anchor_lower + box.anchor_width * config.touch_tolerance_ratio],
        "upper_zone": [box.anchor_upper - box.anchor_width * config.touch_tolerance_ratio,
                       box.anchor_upper + box.anchor_width * config.touch_tolerance_ratio],
        "width_pct": box.width / box.lower * 100,
        "start_date": dates[box.start_index], "end_date": dates[end],
        "observed_until": dates[-1],
        "confirmed_date": dates[box.confirmed_index] if box.confirmed_index is not None else None,
        "duration_bars": duration, "status": box.status,
        "upper_touches": touches(box.upper_touches), "lower_touches": touches(box.lower_touches),
        "missing_conditions": missing if box.status == "candidate" else [], "parent_id": None,
        "previous_box_id": box.previous_box_id,
        "breakout": {"status": box.breakout_status, "direction": box.breakout_direction,
                     "first_date": dates[box.breakout_first] if box.breakout_first is not None else None,
                     "confirmed_date": dates[box.breakout_confirmed]
                     if box.breakout_confirmed is not None else None},
        "events": [{"type": kind, "date": dates[index], "direction": direction}
                   for kind, index, direction in box.events],
        "revisits": [{"kind": kind, "date": dates[index], "boundary": side}
                     for kind, index, side in box.revisits],
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
    # 同一根 K 最多接纳一个已确认方向；细拐点和主拐点不能重复制造访问。
    seen_pivots: set[int] = set()
    fine_config = (replace(config, pivot_left=config.fine_pivot_window,
                           pivot_right=config.fine_pivot_window)
                   if config.fine_pivot_window is not None else None)

    for index, close in enumerate(closes):
        for box in completed:
            _advance_history(box, index, highs[index], lows[index], close, config)
        for box in active:
            _advance(box, index, close, config)
        pivots = _pivots_at(index - config.pivot_right, index, highs, lows, config, confirmed=True)
        if fine_config is not None:
            pivots.extend(_pivots_at(index - fine_config.pivot_right, index, highs, lows,
                                     fine_config, confirmed=True))
        for pivot in sorted(pivots, key=lambda item: item.index):
            if pivot.index in seen_pivots:
                continue
            seen_pivots.add(pivot.index)
            for box in active:
                if box.status in {"candidate", "confirmed"}:
                    _touch(box, pivot, config, high_ranges, low_ranges)
            opposite = [item for item in history if item.side != pivot.side
                        and item.index < pivot.index][-_OPPOSITE_SEEDS:]
            history.append(pivot)
            history.sort(key=lambda item: item.index)
            for first in opposite:
                box = _seed(first, pivot, index, history, active, dates, closes, config,
                            period, high_ranges, low_ranges)
                if box is not None:
                    active.append(box)
        retained = []
        for box in active:
            was_candidate = box.status == "candidate"
            _confirm(box, index, config, closes)
            if was_candidate and box.status == "confirmed":
                previous = [other for other in [*completed, *active]
                            if other.status == "broken"
                            and _same_range(other, box.lower, box.upper, config)]
                if any(other.breakout_confirmed >= box.start_index for other in previous):
                    # 同范围历史已结束，不能靠旧触点把原框换个身份继续延伸。
                    box.status = "invalidated"
                elif previous:
                    box.previous_box_id = max(previous, key=lambda other: other.breakout_confirmed).id
            if was_candidate and box.status == "confirmed" and any(
                other is not box and other.status == "confirmed"
                and other.confirmed_index is not None
                and (other.confirmed_index < index or other in retained)
                and _same_range(other, box.lower, box.upper, config)
                for other in active
            ):
                # 不同种子的中位数可能收敛到同一范围；保留最早确认的身份。
                box.status = "invalidated"
            if box.status == "broken":
                _advance_history(box, index, highs[index], lows[index], close, config, record=False)
                completed.append(box)
            elif box.status != "invalidated":
                retained.append(box)
        active = retained

    # 尾端未确认拐点只为候选提供证据，不影响已形成箱体及历史事件。
    for index in range(max(config.pivot_left, len(dates) - config.pivot_right), len(dates)):
        for pivot in _pivots_at(index, len(dates) - 1, highs, lows, config, confirmed=False):
            if pivot.index in seen_pivots:
                continue
            for box in active:
                if box.status == "candidate":
                    _touch(box, pivot, config, high_ranges, low_ranges)
            opposite = [item for item in history if item.side != pivot.side
                        and item.index < pivot.index][-_OPPOSITE_SEEDS:]
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
