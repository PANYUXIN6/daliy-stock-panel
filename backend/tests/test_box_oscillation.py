"""箱体结构、信息可用时间及固定边界的可复现行为证据。"""
from datetime import date, timedelta
from itertools import pairwise

import polars as pl
import pytest

from app.indicators.box_oscillation import PERIOD_CONFIG, compute_box_oscillation


def prices(values: list[float]) -> pl.DataFrame:
    values = [float(value) for value in values]
    return pl.DataFrame({
        "date": [date(2026, 1, 1) + timedelta(days=i) for i in range(len(values))],
        "open": values, "high": [value + 0.2 for value in values],
        "low": [value - 0.2 for value in values], "close": values,
    })


def wave(anchors: list[tuple[int, float]], end: int | None = None) -> pl.DataFrame:
    values = []
    for (start, first), (stop, last) in pairwise(anchors):
        values.extend(first + (last - first) * step / (stop - start)
                      for step in range(stop - start))
    values.append(anchors[-1][1])
    if end is not None:
        values.extend([values[-1]] * (end + 1 - len(values)))
    return prices(values)


def day(index: int) -> str:
    return (date(2026, 1, 1) + timedelta(days=index)).isoformat()


def outer(boxes: list[dict]) -> dict:
    return next(box for box in boxes if box["upper"] == pytest.approx(110.2)
                and box["lower"] == pytest.approx(99.8))


def established(end: int = 25) -> pl.DataFrame:
    return wave([(0, 105), (2, 110), (6, 100), (10, 110), (14, 100), (18, 110),
                 (22, 100), (25, 105)], end=end)


def test_confirmed_pivots_and_first_confirmation_do_not_repaint_prefix():
    frame = established()
    before = outer(compute_box_oscillation(frame.head(15)))
    assert before["status"] == "candidate"
    assert before["confirmed_date"] is None
    assert before["lower_touches"][-1] == {
        "date": day(14), "price": 99.8, "confirmed_date": None,
    }
    at_confirmation = outer(compute_box_oscillation(frame.head(16)))
    assert at_confirmation["status"] == "confirmed"
    assert at_confirmation["start_date"] == day(2)
    assert at_confirmation["confirmed_date"] == day(15)
    assert at_confirmation["lower_touches"][-1]["confirmed_date"] == day(15)
    extended = outer(compute_box_oscillation(frame))
    assert extended["id"] == at_confirmation["id"]
    assert extended["confirmed_date"] == day(15)
    assert extended["upper"] == at_confirmation["upper"]
    assert extended["lower"] == at_confirmation["lower"]
    assert [touch["date"] for touch in extended["upper_touches"]] == [day(2), day(10), day(18)]


def test_near_edge_cluster_counts_once_until_entire_bar_leaves_band():
    frame = wave([(0, 105), (2, 110), (3, 109.5), (4, 109.6), (5, 109.8),
                  (6, 110), (10, 100), (14, 110), (18, 100), (20, 105)])
    box = outer(compute_box_oscillation(frame))
    assert box["status"] == "confirmed"
    assert [touch["date"] for touch in box["upper_touches"]] == [day(2), day(14)]
    assert [touch["date"] for touch in box["lower_touches"]] == [day(10), day(18)]


def test_duration_is_actual_bars_and_crossing_30_updates_existing_identity():
    frame = established(end=31)
    at_29 = outer(compute_box_oscillation(frame.head(31)))
    at_30 = outer(compute_box_oscillation(frame))
    assert at_29["duration_bars"] == 29
    assert at_29["level"] == "short"
    assert at_30["duration_bars"] == 30
    assert at_30["level"] == "medium"
    assert at_29["id"] == at_30["id"]


def test_time_alone_or_single_side_retests_do_not_confirm():
    # 下沿只有一个已确认访问，即使观察很久，也没有四次交替往返。
    one_low = wave([(0, 105), (2, 110), (6, 100), (10, 110), (14, 105)], end=40)
    box = outer(compute_box_oscillation(one_low))
    assert box["status"] == "candidate"
    assert "下沿不足两次已确认独立试探" in box["missing_conditions"]
    assert "不足四次已确认交替往返" in box["missing_conditions"]
    assert not any(box["status"] == "confirmed"
                   for box in compute_box_oscillation(prices([100 + index / 10 for index in range(100)])))


@pytest.mark.parametrize("anchors", [
    [(0, 105), (2, 110), (6, 100), (10, 114), (14, 104), (18, 118), (22, 108), (26, 122)],
    [(0, 105), (2, 110), (6, 100), (10, 108), (14, 102), (18, 106), (22, 104), (26, 105)],
])
def test_sloped_and_converging_swings_do_not_form_horizontal_box(anchors):
    assert not any(box["status"] == "confirmed" for box in compute_box_oscillation(wave(anchors)))


def test_minimum_daily_length_is_required_after_all_four_touches_confirm():
    frame = prices([105, 110, 100, 110, 100, 105, 105, 105, 105])
    short = outer(compute_box_oscillation(frame.head(8)))
    assert len(short["upper_touches"]) == len(short["lower_touches"]) == 2
    assert short["duration_bars"] == 7
    assert short["status"] == "candidate"
    assert short["missing_conditions"] == ["有效K线不足8根"]
    ready = outer(compute_box_oscillation(frame))
    assert ready["confirmed_date"] == day(8)


def test_nested_inner_breakout_leaves_outer_structure_active():
    frame = wave([(0, 105), (2, 110), (6, 100), (10, 110), (14, 100), (18, 105),
                  (21, 107), (25, 103), (29, 107), (33, 103), (37, 107), (39, 105),
                  (40, 108.5), (41, 108.7)])
    before_breakout = compute_box_oscillation(frame.head(40))
    previous_small = next(box for box in before_breakout if box["upper"] == pytest.approx(107.2)
                          and box["lower"] == pytest.approx(102.8))
    assert outer(before_breakout)["status"] == previous_small["status"] == "confirmed"
    assert previous_small["parent_id"] == outer(before_breakout)["id"]
    boxes = compute_box_oscillation(frame)
    large = outer(boxes)
    small = next(box for box in boxes if box["upper"] == pytest.approx(107.2)
                 and box["lower"] == pytest.approx(102.8))
    assert large["status"] == "confirmed"
    assert large["breakout"]["status"] == "none"
    assert small["status"] == "broken"
    assert small["breakout"] == {
        "status": "confirmed", "direction": "up", "first_date": day(40),
        "confirmed_date": day(41),
    }
    assert small["parent_id"] is not None
    parent = next(box for box in boxes if box["id"] == small["parent_id"])
    assert parent["lower"] <= small["lower"] < small["upper"] <= parent["upper"]
    assert parent["confirmed_date"] is not None
    assert small["end_date"] == day(40)


def test_returned_excursion_and_later_breakout_preserve_boundaries_and_events():
    frame = established()
    extra = prices([113.5, 105, 114, 114.5, 115]).with_columns(
        pl.col("date") + pl.duration(days=frame.height),
    )
    all_bars = pl.concat([frame, extra])
    pending = outer(compute_box_oscillation(all_bars.head(27)))
    assert pending["status"] == "confirmed"
    assert pending["breakout"]["status"] == "pending"
    returned = outer(compute_box_oscillation(all_bars.head(28)))
    assert returned["breakout"]["status"] == "returned"
    broken = outer(compute_box_oscillation(all_bars))
    assert broken["upper"] == 110.2 and broken["lower"] == 99.8
    assert broken["status"] == "broken"
    assert broken["end_date"] == day(28)
    assert broken["observed_until"] == day(30)
    assert broken["duration_bars"] == 27
    assert broken["events"] == [
        {"type": "breakout_pending", "date": day(26), "direction": "up"},
        {"type": "returned", "date": day(27), "direction": "up"},
        {"type": "breakout_pending", "date": day(28), "direction": "up"},
        {"type": "breakout_confirmed", "date": day(29), "direction": "up"},
    ]


def test_wick_alone_does_not_break_and_long_box_does_not_expire_at_120():
    frame = established(end=150).with_columns(
        pl.when(pl.col("date") == date(2026, 1, 1) + timedelta(days=30))
        .then(120.).otherwise(pl.col("high")).alias("high"),
    )
    box = outer(compute_box_oscillation(frame))
    assert box["status"] == "confirmed"
    assert box["level"] == "long"
    assert box["duration_bars"] == 149
    assert box["breakout"]["status"] == "none"


@pytest.mark.parametrize("period,confirmation,minimum,medium,long", [
    ("weekly", 8, 8, 13, 26), ("monthly", 6, 6, 12, 24),
])
def test_week_and_month_use_their_own_pivot_and_duration_defaults(period, confirmation, minimum, medium, long):
    frame = prices([105, 110, 100, 110, 100, 105, 105, 105, 105])
    config = PERIOD_CONFIG[period]
    assert (config.min_bars, config.medium_bars, config.long_bars) == (minimum, medium, long)
    box = outer(compute_box_oscillation(frame, period))
    assert box["period"] == period
    assert box["confirmed_date"] == day(confirmation)
    assert outer(compute_box_oscillation(frame))["confirmed_date"] == day(8)


def test_downward_breakout_is_symmetric_and_invalid_data_is_rejected():
    frame = established()
    tail = prices([97, 96]).with_columns(pl.col("date") + pl.duration(days=frame.height))
    box = outer(compute_box_oscillation(pl.concat([frame, tail])))
    assert box["status"] == "broken"
    assert box["breakout"]["direction"] == "down"
    assert box["breakout"]["first_date"] == day(26)
    assert box["breakout"]["confirmed_date"] == day(27)
    assert compute_box_oscillation(pl.DataFrame()) == []
    with pytest.raises(ValueError, match="有效的正数"):
        compute_box_oscillation(prices([0]))


def test_candidate_medians_accept_price_regions_then_freeze_without_repainting():
    frame = wave([(0, 105), (2, 110), (6, 100), (10, 111.6), (14, 101.7),
                  (18, 109), (22, 100.5), (25, 105)])
    candidate = next(box for box in compute_box_oscillation(frame.head(15))
                     if box['start_date'] == day(2))
    assert candidate['status'] == 'candidate'  # 最新下沿尚未得到右侧K线确认。
    assert candidate['lower'] == pytest.approx(99.8)
    assert candidate['upper'] == pytest.approx((110.2 + 111.8) / 2)
    confirmed = next(box for box in compute_box_oscillation(frame.head(16))
                     if box['id'] == candidate['id'])
    assert confirmed['confirmed_date'] == day(15)
    assert confirmed['lower'] == pytest.approx((99.8 + 101.5) / 2)
    frozen = ('lower', 'upper', 'lower_zone', 'upper_zone', 'confirmed_date')
    for length in range(16, frame.height + 1):
        box = next(box for box in compute_box_oscillation(frame.head(length))
                   if box['id'] == confirmed['id'])
        assert {key: box[key] for key in frozen} == {key: confirmed[key] for key in frozen}
    assert confirmed['lower_zone'] == pytest.approx([97.72, 101.88])
    assert confirmed['upper_zone'] == pytest.approx([108.12, 112.28])


def test_single_recovered_wick_can_seed_range_but_repeated_excursions_cannot():
    frame = established().with_columns(
        pl.when(pl.col('date') == date.fromisoformat(day(4)))
        .then(125.).otherwise(pl.col('high')).alias('high'),
    )
    box = outer(compute_box_oscillation(frame))
    assert box['start_date'] == day(2)
    assert box['confirmed_date'] == day(15)
    repeated = frame.with_columns(
        pl.when(pl.col('date') == date.fromisoformat(day(5)))
        .then(125.).otherwise(pl.col('high')).alias('high'),
    )
    assert not any(box['start_date'] == day(2) and box['upper'] == pytest.approx(110.2)
                   and box['confirmed_date'] is not None
                   for box in compute_box_oscillation(repeated))


def test_daily_fine_pivots_retain_fast_independent_short_swings(monkeypatch):
    from dataclasses import replace

    frame = prices([105, 110, 100, 110, 100, 105, 110, 100, 110,
                    100, 105, 110, 100, 110, 100, 105, 105])
    fine = outer(compute_box_oscillation(frame))
    assert fine['status'] == 'confirmed'
    assert fine['duration_bars'] >= 15
    all_touches = fine['upper_touches'] + fine['lower_touches']
    assert len({touch['date'] for touch in all_touches}) == len(all_touches)
    assert all(touch['confirmed_date'] > touch['date'] for touch in all_touches)
    monkeypatch.setitem(PERIOD_CONFIG, 'daily', replace(PERIOD_CONFIG['daily'], fine_pivot_window=None))
    coarse = outer(compute_box_oscillation(frame))
    assert coarse['confirmed_date'] > fine['confirmed_date']


def test_breakout_buffer_is_independent_and_soft_excursion_is_not_a_return():
    frame = established()
    tail = prices([111., 112., 111.5, 112., 112.1]).with_columns(
        pl.col('date') + pl.duration(days=frame.height),
    )
    combined = pl.concat([frame, tail])
    waiting = outer(compute_box_oscillation(combined.head(29)))
    assert waiting['status'] == 'confirmed'
    assert waiting['breakout']['status'] == 'pending'
    assert waiting['events'] == [{'type': 'breakout_pending', 'date': day(26), 'direction': 'up'}]
    broken = outer(compute_box_oscillation(combined))
    assert broken['breakout']['first_date'] == day(26)
    assert broken['breakout']['confirmed_date'] == day(30)
    assert broken['lower_zone'] == waiting['lower_zone']
    assert broken['upper_zone'] == waiting['upper_zone']
    assert broken['events'][-1] == {'type': 'breakout_confirmed', 'date': day(30), 'direction': 'up'}


def test_seed_history_and_live_box_share_two_close_exit_rule():
    # 尚未组成种子的历史里，一次越界后回箱不能被当成已确认突破。
    frame = established().with_columns(
        pl.when(pl.col('date') == date.fromisoformat(day(4)))
        .then(113.5).otherwise(pl.col('close')).alias('close'),
        pl.when(pl.col('date') == date.fromisoformat(day(4)))
        .then(113.7).otherwise(pl.col('high')).alias('high'),
    )
    box = outer(compute_box_oscillation(frame))
    assert box['start_date'] == day(2)
    assert box['confirmed_date'] == day(15)
    persistent = frame.with_columns(
        pl.when(pl.col('date') == date.fromisoformat(day(5)))
        .then(113.5).otherwise(pl.col('close')).alias('close'),
        pl.when(pl.col('date') == date.fromisoformat(day(5)))
        .then(113.7).otherwise(pl.col('high')).alias('high'),
    )
    assert not any(box['start_date'] == day(2) and box['upper'] == pytest.approx(110.2)
                   and box['confirmed_date'] is not None
                   for box in compute_box_oscillation(persistent))


def test_jumping_to_opposite_side_does_not_report_a_close_inside_box():
    frame = established()
    tail = prices([111., 99., 105.]).with_columns(pl.col('date') + pl.duration(days=frame.height))
    box = outer(compute_box_oscillation(pl.concat([frame, tail])))
    assert box['status'] == 'confirmed'
    assert box['events'] == [
        {'type': 'breakout_pending', 'date': day(26), 'direction': 'up'},
        {'type': 'breakout_pending', 'date': day(27), 'direction': 'down'},
        {'type': 'returned', 'date': day(28), 'direction': 'down'},
    ]


@pytest.mark.parametrize('direction', ['up', 'down'])
def test_historical_revisits_rearm_after_leaving_each_zone_inside_box(direction):
    frame = established()
    # 回箱后离开上下沿区域；贴边期间即使收盘离开，影线仍相交也不能重复计数。
    tail = prices([114., 115., 109., 105., 109., 108., 109.,
                   105., 101., 102., 101., 105., 109.]).with_columns(
        pl.col('date') + pl.duration(days=frame.height),
    )
    full = pl.concat([frame, tail])
    if direction == 'down':
        full = full.with_columns((220. - pl.col('open')).alias('open'),
                                 (220. - pl.col('low')).alias('high'),
                                 (220. - pl.col('high')).alias('low'),
                                 (220. - pl.col('close')).alias('close'))
    boundary = 'upper' if direction == 'up' else 'lower'
    opposite = 'lower' if direction == 'up' else 'upper'
    expected = [
        {'kind': 'entered', 'date': day(28), 'boundary': boundary},
        {'kind': 'touch', 'date': day(30), 'boundary': boundary},
        {'kind': 'touch', 'date': day(34), 'boundary': opposite},
        {'kind': 'touch', 'date': day(38), 'boundary': boundary},
    ]
    for length in range(28, full.height + 1):
        historical = next(box for box in compute_box_oscillation(full.head(length))
                          if box['confirmed_date'] == day(15))
        assert historical['revisits'] == [event for event in expected
                                         if event['date'] <= day(length - 1)]


@pytest.mark.parametrize('direction', ['up', 'down'])
def test_long_delayed_revisits_preserve_ended_box_and_new_box_needs_fresh_touches(direction):
    frame = established()
    # 远离150根后先访问旧区域，再重新进入；连续贴边只记录一次区域回访。
    tail = prices([114., 115.] + [120.] * 150 + [112.1, 112., 112.1, 109., 108., 105.]
                  + [110., 100., 110., 100., 105., 110., 100., 110., 100., 105.]).with_columns(
        pl.col('date') + pl.duration(days=frame.height),
    )
    full = pl.concat([frame, tail])
    if direction == 'down':
        full = full.with_columns((220. - pl.col('open')).alias('open'),
                                 (220. - pl.col('low')).alias('high'),
                                 (220. - pl.col('high')).alias('low'),
                                 (220. - pl.col('close')).alias('close'))
    broken = next(box for box in compute_box_oscillation(full.head(28))
                  if box['confirmed_date'] == day(15))
    boxes = compute_box_oscillation(full)
    historical = next(box for box in boxes if box['id'] == broken['id'])
    frozen = ['status', 'confirmed_date', 'end_date', 'duration_bars', 'lower', 'upper',
              'lower_zone', 'upper_zone', 'upper_touches', 'lower_touches', 'breakout', 'events']
    assert {key: historical[key] for key in frozen} == {key: broken[key] for key in frozen}
    assert historical['breakout']['direction'] == direction
    boundary = 'upper' if direction == 'up' else 'lower'
    assert historical['revisits'][:2] == [
        {'kind': 'touch', 'date': day(178), 'boundary': boundary},
        {'kind': 'entered', 'date': day(181), 'boundary': boundary},
    ]
    before_new_structure = compute_box_oscillation(full.head(184))
    assert not any(box['previous_box_id'] == historical['id'] for box in before_new_structure)
    renewed = [box for box in boxes if box['previous_box_id'] == historical['id']]
    assert len(renewed) == 1
    assert renewed[0]['id'] != historical['id']
    assert renewed[0]['start_date'] > historical['breakout']['confirmed_date']
    assert all(touch['date'] > historical['breakout']['confirmed_date']
               for touch in renewed[0]['upper_touches'] + renewed[0]['lower_touches'])
