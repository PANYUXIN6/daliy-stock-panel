"""把分析输入无损编码为表格，避免逐行重复字段名占用模型上下文。"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

TABLE_FORMAT_HINT = (
    "表格编码说明：columns 为列名，rows 中每行按 columns 顺序对应，行顺序不变；"
    "rows 为空表示该表无数据。字段不一致的表保留原始对象列表。null 或缺失字段均不代表 0，"
    "数值与单位保持原样。"
)


def tabular_records(records: Sequence[dict[str, Any]]) -> dict[str, Any] | list[dict[str, Any]]:
    """统一字段的表仅发送一次列名；异构记录保留原形，不推断缺失值。"""
    columns = list(records[0]) if records else []
    if records and any(record.keys() != records[0].keys() for record in records[1:]):
        return list(records)
    return {
        "columns": columns,
        "rows": [[record[column] for column in columns] for record in records],
    }
