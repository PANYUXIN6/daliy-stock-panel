"""报告提示词保留完整数据、减少重复字段；费用样本不是运行时上限。"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

import polars as pl
import pytest
from tickflow.generated_model import (
    BalanceSheetRecord,
    CashFlowRecord,
    IncomeRecord,
    MetricsRecord,
    SharesRecord,
)

from app.services import financial_analyzer, stock_analyzer
from app.services.ai_credit_policy import CreditPolicy, token_cost
from app.services.ai_credits import CreditStore
from app.services.ai_metering import text_input_tokens_upper
from app.services.ai_prompt_data import tabular_records
from app.services.auth import AuthStore
from app.services.user_context import UserContext


@pytest.fixture
def ledger(tmp_path):
    accounts = AuthStore(tmp_path)
    accounts.bootstrap("admin", "initial-password")
    guest = accounts.create_user("guest", "temporary-password")
    token, _ = accounts.login("guest", "temporary-password", "test", "")
    token, _ = accounts.change_password(token, "temporary-password", "changed-password")
    owner = UserContext(guest["id"], "guest", tmp_path, session_token=token)
    return CreditStore(tmp_path), owner


def full_financials():
    schemas = {
        "balance_sheet": BalanceSheetRecord, "cash_flow": CashFlowRecord,
        "metrics": MetricsRecord, "income": IncomeRecord, "shares": SharesRecord,
    }
    periods = ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"]
    tables = {}
    for name, schema in schemas.items():
        rows = []
        for index, period in enumerate(periods):
            row = {
                column: 12.3456 if name == "metrics" or "eps" in column else 123456789.0123
                for column in schema.__annotations__
            }
            row.update({
                "period_end": period, "announce_date": "2026-08-20",
                "source_update_date": "2026-08-20", "source": "custom",
                "date_basis": "source_update", "disclosure_type": "formal",
            })
            if index == 1:
                numeric = next(column for column in row if column not in {"period_end", "announce_date"})
                row[numeric] = None
            rows.append(row)
        tables[name] = rows
    return tables


def full_kline():
    rows = []
    for index in range(90):
        row = {}
        for column in stock_analyzer._KLINE_KEEP_COLS:
            if column == "date":
                value = date(2026, 5, 1) + timedelta(days=index)
            elif column.startswith("signal_"):
                value = False
            elif column == "consecutive_limit_ups":
                value = index % 3
            elif column == "volume":
                value = 1234567
            else:
                value = 12.3456
            row[column] = value
        rows.append(row)
    frame = pl.DataFrame(rows)
    return frame, stock_analyzer._clean_rows(frame, stock_analyzer._KLINE_KEEP_COLS)


def decode_table(table):
    if isinstance(table, list):
        return table
    return [dict(zip(table["columns"], row, strict=True)) for row in table["rows"]]


def json_blocks(prompt):
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", prompt, re.DOTALL)]


def assert_prompt_cost_sample(ledger, feature, system_prompt, user_prompt):
    policy = CreditPolicy()
    upper = text_input_tokens_upper([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ], None)
    cost = token_cost(upper, policy.input_micros_per_million) + token_cost(2048, policy.output_micros_per_million)
    assert cost <= 100_000, f"{feature}: 输入上界 {upper} tokens，含 2048 输出 tokens 共 {cost} 微元"
    store, owner = ledger
    task = store.begin_task(owner, feature)
    call = task.reserve_call(upper, 2048)
    assert call.max_tokens == 2048
    assert call.reserved_micros == 0  # 默认仅记录用量，不再预留或限制金额。
    task.settle_call(call.id, 0)
    task.finish("failed")


def test_tabular_encoding_preserves_order_values_and_json_types():
    records = [
        {"date": "2026-10-20", "close": 12.3456, "signal": False, "boards": 0, "extra": None},
        {"date": "2026-10-19", "close": 0.0, "signal": True, "boards": 2, "extra": "中文\n说明"},
    ]
    encoded = json.loads(json.dumps(tabular_records(records), ensure_ascii=False, separators=(",", ":")))
    assert encoded["columns"] == list(records[0])
    assert decode_table(encoded) == records
    assert type(encoded["rows"][0][2]) is bool
    assert type(encoded["rows"][0][3]) is int


def test_heterogeneous_records_keep_missing_null_and_zero_distinct():
    records = [{"profit": None}, {"profit": 0, "note": "新增披露字段"}, {}]
    encoded = json.loads(json.dumps(tabular_records(records), ensure_ascii=False, separators=(",", ":")))
    assert encoded == records
    assert "profit" not in encoded[2]
    assert encoded[0]["profit"] is None
    assert encoded[1]["profit"] == 0


def test_empty_tables_remain_explicit_in_both_report_prompts():
    financial = financial_analyzer._build_user_prompt({"metrics": [], "income": []}, "600000.SH", "")
    table = json_blocks(financial)[0]
    assert table == {"metrics": {"columns": [], "rows": []}, "income": {"columns": [], "rows": []}}
    assert "rows 为空表示该表无数据" in financial
    stock = stock_analyzer._build_user_prompt([], {}, {}, 12.3456, "600000.SH", "")
    assert json_blocks(stock)[0] == {"columns": [], "rows": []}
    assert "暂无财务数据" in stock


def test_full_90_row_stock_prompt_preserves_data_and_stays_compact(ledger):
    frame, kline = full_kline()
    assert frame.width == 35
    financials = {name: rows for name, rows in full_financials().items() if name in {"metrics", "income"}}
    levels = stock_analyzer.compute_levels(frame)
    prompt = stock_analyzer._build_user_prompt(
        kline, financials, levels, 12.3456, "600000.SH", "分析量价变化和关键价位",
    )
    encoded_kline, encoded_financials = json_blocks(prompt)
    assert encoded_kline["columns"] == stock_analyzer._KLINE_KEEP_COLS
    assert decode_table(encoded_kline) == kline
    assert {name: decode_table(table) for name, table in encoded_financials.items()} == financials
    assert "金额单位为元" in prompt
    assert_prompt_cost_sample(ledger, "stock_analysis", stock_analyzer._SYSTEM_PROMPT, prompt)


def test_full_financial_prompt_preserves_all_four_periods_and_stays_compact(ledger):
    financials = full_financials()
    prompt = financial_analyzer._build_user_prompt(financials, "600000.SH", "核对现金流和盈利质量")
    encoded = json_blocks(prompt)[0]
    assert list(encoded) == list(financials)
    for name, table in encoded.items():
        assert len(table["rows"]) == 4
        assert table["columns"] == list(financials[name][0])
        assert decode_table(table) == financials[name]
    assert "金额单位为元,比率类指标为百分点" in prompt
    assert_prompt_cost_sample(ledger, "financial_analysis", financial_analyzer._SYSTEM_PROMPT, prompt)
