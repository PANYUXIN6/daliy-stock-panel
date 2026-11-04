"""参数网格 {min,max,step} 展开不得越过用户填写的上限。

(hi-lo) 不是 step 整数倍时, 旧实现用 round() 算步数会向上取整, 多造一个
超过 max 的候选值: 1~20 步长 7 展开成 [1,8,15,22], 22 又被参数自身的
range 校验拒绝 —— 用户填的上限就是参数合法上限, 却直接报错扫不了。
"""
from __future__ import annotations

import pytest

from app.backtest.optimizer import count_combinations, expand_param_grid

PARAMS_META = [
    {"id": "min_boards", "type": "int", "default": 2, "min": 1, "max": 20, "step": 1},
    {"id": "ma_proximity", "type": "float", "default": 0.02, "min": 0.01, "max": 0.50, "step": 0.005},
]


def test_int_range_not_divisible_by_step_stays_within_max():
    """1~20 步长 7: 不得造出 22, 也不得因此报「超出范围」。"""
    combos = expand_param_grid(PARAMS_META, {"min_boards": {"min": 1, "max": 20, "step": 7}})
    values = sorted(combo["min_boards"] for combo in combos)
    assert values == [1, 8, 15]
    assert count_combinations(PARAMS_META, {"min_boards": {"min": 1, "max": 20, "step": 7}}) == 3


def test_float_range_not_divisible_by_step_stays_within_max():
    """0.01~0.05 步长 0.015: 末候选 0.055 越过用户填的 0.05。"""
    combos = expand_param_grid(
        PARAMS_META, {"ma_proximity": {"min": 0.01, "max": 0.05, "step": 0.015}}
    )
    values = sorted(combo["ma_proximity"] for combo in combos)
    assert values == pytest.approx([0.01, 0.025, 0.04])
    assert max(values) <= 0.05
