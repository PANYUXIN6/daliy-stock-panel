"""离线边界评估的匹配、分母和有效 K 线时间口径。"""
from datetime import date, timedelta

import pytest

from app.indicators.box_evaluation import evaluate_structures

DATES = [(date(2026, 1, 2) + timedelta(days=3 * index)).isoformat() for index in range(40)]


def structure(identifier, start=0, end=9, lower=100, upper=110, confirmed=5):
    return {"id": identifier, "start_date": DATES[start], "end_date": DATES[end],
            "lower": lower, "upper": upper,
            "confirmed_date": DATES[confirmed] if confirmed is not None else None}


def evaluate(detections, references, negatives=None, config=None):
    return evaluate_structures(detections, references, negatives or [], DATES, config)


def test_global_assignment_maximizes_count_before_minimizing_cost_and_ignores_input_order():
    # 最便宜的 flexible→first 会吃掉 constrained 唯一可配的参考，不能贪心。
    detections = [structure("flexible"), structure("constrained", end=8)]
    references = [structure("first"), structure("second", start=5, end=14)]
    config = {"min_time_iou": 0.3}
    result = evaluate(detections, references, config=config)
    assert {(item["detection_id"], item["reference_id"]) for item in result["matches"]} == {
        ("constrained", "first"), ("flexible", "second")}
    assert result["counts"]["true_positive"] == 2
    assert result == evaluate(list(reversed(detections)), list(reversed(references)), config=config)


def test_assignment_minimizes_total_error_among_equal_cardinality_matches():
    result = evaluate([structure("a", lower=100.1, upper=110.1), structure("b")],
                      [structure("first"), structure("second", lower=101, upper=111)])
    assert {(item["detection_id"], item["reference_id"]) for item in result["matches"]} == {
        ("b", "first"), ("a", "second")}
    assert sum(item["cost"] for item in result["matches"]) == pytest.approx(0.09)


def test_bar_distances_iou_and_historical_broken_confirmation_are_explicit():
    detection = {**structure("det", start=1, end=10, lower=102, upper=112, confirmed=7),
                 "status": "broken"}
    reference = structure("ref")
    result = evaluate([detection], [reference])
    match = result["matches"][0]
    assert match["boundary_error"] == 0.2
    assert match["time_iou"] == pytest.approx(9 / 11)
    assert (match["start_error_bars"], match["end_error_bars"]) == (1, 1)
    assert evaluate([detection], [reference], config={"max_boundary_error": 0.19})["counts"]["true_positive"] == 0
    assert evaluate([detection], [reference], config={"max_start_error_bars": 0})["counts"]["true_positive"] == 0


def test_optional_earliest_confirmation_metadata_does_not_affect_boundary_assessment():
    detection, reference = structure("det"), structure("ref")
    result = evaluate([detection], [reference])
    for earliest in (None, "2020-01-01", DATES[5]):
        assert evaluate([detection], [{**reference, "earliest_confirmable_date": earliest}]) == result


def test_candidates_are_separate_and_empty_denominators_stay_unknown():
    result = evaluate([structure("candidate", confirmed=None)], [])
    assert result["candidate_ids"] == ["candidate"]
    assert result["counts"]["candidates"] == 1
    assert result["counts"]["false_positive"] == 0
    assert all(value is None for value in result["rates"].values())
    assert evaluate([], [structure("ref")])["rates"]["false_negative_rate"] == 1


def test_precision_recall_and_negative_segment_fpr_have_distinct_denominators():
    result = evaluate(
        [structure("matched"), structure("unmatched", start=20, end=29, confirmed=25),
         structure("duplicate", start=20, end=29, confirmed=25),
         structure("candidate", start=30, end=39, confirmed=None)],
        [structure("reference"), structure("missed", start=10, end=19, confirmed=15)],
        [{"id": "negative_hit", "start_date": DATES[28], "end_date": DATES[32]},
         {"id": "negative_clean", "start_date": DATES[33], "end_date": DATES[39]}],
    )
    assert result["rates"] == {"precision": 1 / 3, "false_discovery_rate": 2 / 3,
                               "recall": 0.5, "false_negative_rate": 0.5,
                               "negative_segment_false_positive_rate": 0.5}
    assert result["negative_segments"] == [
        {"id": "negative_clean", "detection_ids": []},
        {"id": "negative_hit", "detection_ids": ["duplicate", "unmatched"]}]


def test_one_reference_can_only_match_one_prediction():
    result = evaluate([structure("first"), structure("second")], [structure("ref")])
    assert result["counts"]["true_positive"] == 1
    assert result["counts"]["false_positive"] == 1


@pytest.mark.parametrize("change, config", [
    ({"upper": float("nan")}, None),
    ({"lower": 111}, None),
    ({"start_date": "2026-01-03"}, None),
    ({}, {"max_start_error_bars": -1}),
    ({}, {"min_time_iou": 1.1}),
])
def test_invalid_label_coordinates_and_configuration_fail_explicitly(change, config):
    with pytest.raises(ValueError):
        evaluate([{**structure("det"), **change}], [], config=config)


def test_inconsistent_timeline_or_duplicate_label_ids_are_rejected():
    with pytest.raises(ValueError, match="严格递增"):
        evaluate_structures([], [], [], list(reversed(DATES)))
    with pytest.raises(ValueError, match="唯一"):
        evaluate([structure("same"), structure("same")], [])

