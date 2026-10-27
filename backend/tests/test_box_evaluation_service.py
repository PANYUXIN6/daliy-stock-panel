"""保护评估分母、时间隔离与离线文件边界，期望标签不取自引擎输出。"""
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.box_evaluation import EvaluationDataset, evaluate_dataset
from scripts.evaluate_box_oscillation import main

FIXTURE = Path(__file__).parent / "fixtures/box_oscillation/synthetic_baseline_v1.json"


def bars(identifier="daily_first_confirmation"):
    suite = json.loads(FIXTURE.read_text())
    return next(case["bars"] for case in suite["cases"] if case["id"] == identifier)


def dataset():
    # 人工手算的矩形示例：2/6/10/14 为交替触点，第16根索引确认。
    # 这里只标 synthetic，不把回归 fixture 的 expected_boxes 当真值。
    return {
        "schema_version": 1, "dataset_id": "test-synthetic", "description": "工程测试",
        "split_plan": {
            "gap_bars": 0,
            "splits": [
                {"name": "calibration", "start_date": "2024-01-01", "end_date": "2024-02-09"},
                {"name": "validation", "start_date": "2024-02-12", "end_date": "2024-03-15"},
            ],
        },
        "match_config": {},
        "cases": [{
            "id": "rectangle", "symbol": "SYNTHETIC", "asset_type": "stock", "period": "daily",
            "price_basis": "qfq", "bars_are_completed": True, "label_source": "synthetic",
            "annotation_status": "complete",
            "evaluation_interval": {"start_date": "2024-01-02", "end_date": "2024-01-24"},
            "bars": bars(),
            "references": [{"id": "manual-rectangle", "start_date": "2024-01-04", "end_date": "2024-01-24",
                            "lower": 99.8, "upper": 110.2}],
            "negative_intervals": [],
        }],
    }


def run(payload):
    return evaluate_dataset(EvaluationDataset.model_validate(payload))


def test_real_engine_matches_independent_synthetic_reference_and_keeps_sources_separate():
    payload = dataset()
    human = deepcopy(payload["cases"][0])
    human.update(id="human-copy-for-contract-test", label_source="human", annotator="test-only")
    payload["cases"].append(human)
    report = run(payload)
    for source in ("human", "synthetic"):
        summary = report["summary"][source]["calibration"]["daily"]
        assert summary["counts"]["true_positive"] == 1
        assert summary["counts"]["false_positive"] == 0
        assert summary["match_quality"]["boundary_error"]["mean"] == 0
        assert summary["match_quality"]["time_iou"]["mean"] == 1
    assert report["quality_conclusion"] == "not_established"
    assert set(report["summary"]["human"]) == {"calibration", "validation"}


def test_unannotated_snapshot_is_not_scored_as_false_positive_or_quality():
    payload = dataset()
    payload["cases"][0].update(annotation_status="pending", label_source="human")
    report = run(payload)
    assert report["calibration_status"] == "awaiting_human_annotations"
    assert report["case_reports"] == []
    assert report["summary"]["human"]["calibration"]["daily"]["rates"]["recall"] is None
    assert report["skipped_cases"] == [{"case_id": "rectangle", "reason": "awaiting_annotations"}]


def test_recognition_uses_reviewed_cutoff_prefix_not_later_confirmation():
    payload = dataset()
    case = payload["cases"][0]
    case["evaluation_interval"]["end_date"] = "2024-01-23"
    case["references"][0].update(end_date="2024-01-23", earliest_confirmable_date=None)
    report = run(payload)["case_reports"][0]
    assert report["recognition_as_of"] == "2024-01-23"
    assert report["structure"]["counts"]["confirmed_detections"] == 0
    assert report["structure"]["counts"]["candidates"] == 1
    assert report["structure"]["counts"]["false_negative"] == 1


def test_optional_confirmation_note_does_not_change_boundary_evaluation():
    payload = dataset()
    before = run(payload)["summary"]
    payload["cases"][0]["references"][0]["earliest_confirmable_date"] = "2020-01-01"
    assert run(payload)["summary"] == before


def test_cross_boundary_parent_purges_nested_child_in_another_period():
    payload = dataset()
    splits = payload["split_plan"]["splits"]
    splits[0]["end_date"] = "2024-01-24"
    splits[1]["start_date"] = "2024-01-25"
    case = payload["cases"][0]
    case["bars"] = bars("daily_time_level_transition")
    case["evaluation_interval"]["end_date"] = "2024-02-14"
    case["references"][0]["end_date"] = "2024-02-09"
    child = deepcopy(case)
    child.update(id="weekly-child", period="weekly", bars=bars("weekly_first_confirmation"),
                 evaluation_interval={"start_date": "2024-01-05", "end_date": "2024-03-01"},
                 references=[{"id": "nested", "start_date": "2024-01-26", "end_date": "2024-02-02",
                              "lower": 102., "upper": 108.}])
    payload["cases"].append(child)
    report = run(payload)
    for item in report["case_reports"]:
        assert item["structure"]["counts"]["references"] == 0
        assert item["purged_reference_ids"] == (["nested"] if item["case_id"] == "weekly-child"
                                                else ["manual-rectangle"])


def test_gap_uses_actual_bars_and_marks_near_boundary_reference_purged():
    payload = dataset()
    payload["cases"][0]["bars"] = bars("daily_time_level_transition")
    payload["cases"][0]["evaluation_interval"]["end_date"] = "2024-02-14"
    payload["split_plan"]["splits"][0]["end_date"] = "2024-01-25"
    payload["split_plan"]["splits"][1]["start_date"] = "2024-01-26"
    payload["split_plan"]["gap_bars"] = 2
    report = run(payload)
    calibration = next(item for item in report["case_reports"] if item["split"] == "calibration")
    validation = next(item for item in report["case_reports"] if item["split"] == "validation")
    assert calibration["reviewed_core"]["end_date"] == "2024-01-23"
    assert validation["reviewed_core"]["start_date"] == "2024-01-30"
    assert calibration["purged_reference_ids"] == ["manual-rectangle"]


def test_two_splits_evaluate_validation_without_additional_discipline_fields():
    payload = dataset()
    payload["split_plan"]["splits"] = [
        {"name": "calibration", "start_date": "2020-01-01", "end_date": "2023-12-31"},
        {"name": "validation", "start_date": "2024-01-01", "end_date": "2024-12-31"},
    ]
    report = run(payload)
    assert len(report["case_reports"]) == 1
    assert report["case_reports"][0]["split"] == "validation"


@pytest.mark.parametrize("mutation", [
    lambda case: case.update(evaluation_interval=None),
    lambda case: case.update(label_source="review_candidate"),
    lambda case: case["bars"].append(deepcopy(case["bars"][-1])),
    lambda case: case["bars"][0].update(close=float("nan")),
    lambda case: case.update(price_basis="index"),
    lambda case: case.update(negative_intervals=[{"id": "negative", "start_date": "2024-01-04",
                                                "end_date": "2024-01-10", "reason": "conflicts"}]),
])
def test_invalid_annotation_or_price_contract_fails_closed(mutation):
    payload = dataset()
    mutation(payload["cases"][0])
    with pytest.raises(ValidationError):
        EvaluationDataset.model_validate(payload)


def test_cli_outputs_template_report_without_touching_input_and_rejects_overwrite(tmp_path):
    template = Path(__file__).resolve().parents[2] / "docs/examples/box-evaluation-template.json"
    original = template.read_bytes()
    output = tmp_path / "report.json"
    assert main(["evaluate", "--dataset", str(template), "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["calibration_status"] == "awaiting_human_annotations"
    assert template.read_bytes() == original
    before = output.read_bytes()
    with pytest.raises(SystemExit) as exc:
        main(["schema", "--output", str(output)])
    assert exc.value.code == 2
    assert output.read_bytes() == before


def test_cli_reports_changed_baseline_as_failure_and_rejects_empty_evidence(tmp_path):
    suite = json.loads(FIXTURE.read_text())
    suite["cases"] = suite["cases"][:1]
    suite["cases"][0]["expected_boxes"][0]["lower"] += 1
    source = tmp_path / "baseline.json"
    source.write_text(json.dumps(suite))
    output = tmp_path / "changed-report.json"
    assert main(["baseline", "--baseline", str(source), "--output", str(output)]) == 1
    report = json.loads(output.read_text())
    assert report["cases"][0]["different_fields"] == ["$[0].lower"]
    suite["cases"] = []
    source.write_text(json.dumps(suite))
    with pytest.raises(SystemExit) as exc:
        main(["baseline", "--baseline", str(source), "--output", str(tmp_path / "empty.json")])
    assert exc.value.code == 2
    assert not (tmp_path / "empty.json").exists()
