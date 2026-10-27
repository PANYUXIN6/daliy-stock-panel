"""冻结既有默认输出，防止后续诊断字段开发意外改变价格结构与事件。

期望值来自升级前引擎，是行为回归证据，不是人工标注或识别质量金标准。
输入均为合成的已完成有效 K；周/月用例不覆盖日线聚合服务。
"""
import json
from dataclasses import asdict
from pathlib import Path

import polars as pl
import pytest

from app.indicators.box_oscillation import PERIOD_CONFIG, compute_box_oscillation

_BASELINE = json.loads(
    (Path(__file__).parent / "fixtures/box_oscillation/synthetic_baseline_v1.json")
    .read_text(encoding="utf-8")
)


def test_frozen_default_configuration():
    assert {period: asdict(config) for period, config in PERIOD_CONFIG.items()} == (
        _BASELINE["default_configs"]
    )


@pytest.mark.parametrize("case", _BASELINE["cases"], ids=lambda case: case["id"])
def test_complete_default_output_matches_frozen_regression_baseline(case):
    actual = compute_box_oscillation(pl.DataFrame(case["bars"]), case["period"])
    assert actual == case["expected_boxes"]
