"""缺失日线仅能由目标交易日的明确停牌或未上市证据解释。"""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.data_providers import trading_status

DAY = date(2026, 10, 21)
HALT_REPORT = "RPT_CUSTOM_SUSPEND_DATA_INTERFACE"
IPO_REPORT = "RPTA_APP_IPOAPPLY"


def envelope(rows, *, pages=1, count=None):
    return {
        "success": True,
        "code": 0,
        "result": {"pages": pages, "count": len(rows) if count is None else count, "data": rows},
    }


def halt_row(symbol="000016.SZ", **overrides):
    row = {
        "SECURITY_CODE": symbol.split(".")[0],
        "SECUCODE": symbol,
        "SUSPEND_START_TIME": "2026-09-04 09:30:00",
        "SUSPEND_END_TIME": None,
        "SUSPEND_EXPIRE": "连续停牌",
        "PREDICT_RESUME_DATE": None,
    }
    return row | overrides


def ipo_row(code="001381", **overrides):
    row = {
        "SECURITY_CODE": code,
        "TRADE_MARKET": "深圳证券交易所",
        "TRADE_MARKET_CODE": "069001002001",
        "APPLY_DATE": "2026-10-19 00:00:00",
        "BALLOT_PAY_DATE": "2026-10-21 00:00:00",
        "LISTING_DATE": None,
        "CONTINUOUS_1WORD_NUM": "待上市",
    }
    return row | overrides


@pytest.fixture
def source(monkeypatch):
    """实际 httpx 请求编码与返回解析参与测试，全程使用内存 transport。"""
    original_client = httpx.Client
    requests = []
    payloads = {HALT_REPORT: envelope([]), IPO_REPORT: envelope([])}

    def handle(request):
        requests.append(request)
        assert str(request.url).startswith("https://datacenter-web.eastmoney.com/api/data/v1/get?")
        payload = payloads[request.url.params["reportName"]]
        if callable(payload):
            payload = payload(request)
        if isinstance(payload, Exception):
            raise payload
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        trading_status.httpx, "Client",
        lambda **_: original_client(transport=httpx.MockTransport(handle), trust_env=False),
    )
    monkeypatch.setattr(trading_status, "cn_today", lambda: date(2026, 10, 22), raising=False)
    return payloads, requests


@pytest.mark.parametrize("changes,expected", [
    ({}, True),
    ({"SUSPEND_START_TIME": "2026-10-21 09:30:00", "SUSPEND_END_TIME": "2026-10-21 15:00:00"}, True),
    ({"SUSPEND_START_TIME": "2026-10-22 09:30:00"}, False),
    ({"SUSPEND_END_TIME": "2026-10-20 15:00:00"}, False),
    ({"PREDICT_RESUME_DATE": "2026-10-21 00:00:00"}, False),
    ({"SUSPEND_START_TIME": "2026-10-21 10:00:00"}, False),
    ({"SUSPEND_END_TIME": "2026-10-21 11:30:00"}, False),
    ({"SUSPEND_START_TIME": None}, False),
])
def test_only_full_day_suspensions_cover_missing_daily(source, changes, expected):
    payloads, requests = source
    payloads[HALT_REPORT] = envelope([halt_row(**changes)])
    status = trading_status.get_daily_trading_status(DAY, ["000016.SZ"])
    assert status.day == DAY
    assert status.source == "eastmoney"
    assert status.halted == (frozenset({"000016.SZ"}) if expected else frozenset())
    assert status.not_listed == frozenset()
    suspend_request = next(r for r in requests if r.url.params["reportName"] == HALT_REPORT)
    assert "2026-10-21" in suspend_request.url.params["filter"]


def test_next_day_resume_preserves_last_suspended_day(source):
    payloads, _ = source
    payloads[HALT_REPORT] = envelope([halt_row(
        "002388.SZ", SUSPEND_START_TIME="2026-10-08 09:30:00",
        SUSPEND_END_TIME="2026-10-21 15:00:00", PREDICT_RESUME_DATE="2026-10-22 00:00:00",
    )])
    status = trading_status.get_daily_trading_status(DAY, ["002388.SZ"])
    assert status.halted == frozenset({"002388.SZ"})


@pytest.mark.parametrize("changes,expected", [
    ({"LISTING_DATE": "2026-10-22 00:00:00", "CONTINUOUS_1WORD_NUM": None}, True),
    ({"LISTING_DATE": "2026-10-21 00:00:00", "CONTINUOUS_1WORD_NUM": None}, False),
    ({"LISTING_DATE": "2026-10-20 00:00:00"}, False),
    ({}, True),
    ({"LISTING_DATE": "1970-01-01 00:00:00", "CONTINUOUS_1WORD_NUM": None,
      "BALLOT_PAY_DATE": "2026-10-13 00:00:00"}, False),
    ({"LISTING_DATE": None, "CONTINUOUS_1WORD_NUM": None,
      "BALLOT_PAY_DATE": "2026-10-13 00:00:00"}, False),
    ({"APPLY_DATE": None}, False),
])
def test_ipo_requires_explicit_not_yet_listed_evidence(source, changes, expected):
    payloads, _ = source
    payloads[IPO_REPORT] = envelope([ipo_row(**changes)])
    status = trading_status.get_daily_trading_status(DAY, ["001381.SZ"])
    assert status.not_listed == (frozenset({"001381.SZ"}) if expected else frozenset())
    assert status.halted == frozenset()


def test_evidence_is_restricted_to_requested_symbols(source):
    payloads, _ = source
    payloads[HALT_REPORT] = envelope([halt_row(), halt_row("200016.SZ")])
    payloads[IPO_REPORT] = envelope([ipo_row(), ipo_row("301718", LISTING_DATE="2026-10-22")])
    status = trading_status.get_daily_trading_status(DAY, ["000016.SZ", "001381.SZ"])
    assert status.halted == frozenset({"000016.SZ"})
    assert status.not_listed == frozenset({"001381.SZ"})


def test_ipo_market_mismatch_cannot_exempt_same_bare_code(source):
    payloads, _ = source
    payloads[IPO_REPORT] = envelope([ipo_row("001381")])
    status = trading_status.get_daily_trading_status(DAY, ["001381.SH"])
    assert status.not_listed == frozenset()


def test_current_status_cannot_be_used_for_future_target_day(source):
    _, requests = source
    with pytest.raises(trading_status.TradingStatusError):
        trading_status.get_daily_trading_status(date(2026, 10, 23), ["000016.SZ"])
    assert requests == []


def test_all_report_pages_are_collected_before_evidence_is_returned(source):
    payloads, requests = source

    def page(request):
        number = int(request.url.params["pageNumber"])
        return envelope([halt_row("000016.SZ" if number == 1 else "002388.SZ")], pages=2, count=2)

    payloads[HALT_REPORT] = page
    status = trading_status.get_daily_trading_status(DAY, ["000016.SZ", "002388.SZ"])
    assert status.halted == frozenset({"000016.SZ", "002388.SZ"})
    assert [r.url.params["pageNumber"] for r in requests if r.url.params["reportName"] == HALT_REPORT] == ["1", "2"]


def test_page_total_change_discards_earlier_evidence(source):
    payloads, _ = source

    def page(request):
        number = int(request.url.params["pageNumber"])
        return envelope([halt_row()], pages=2, count=2 if number == 1 else 3)

    payloads[HALT_REPORT] = page
    with pytest.raises(trading_status.TradingStatusError):
        trading_status.get_daily_trading_status(DAY, ["000016.SZ"])


@pytest.mark.parametrize("payload", [
    {"success": False, "code": 500, "result": None},
    {"success": True, "code": 0, "result": None},
    envelope([halt_row()], count=2),
    envelope([halt_row()], pages=0),
    envelope([halt_row()], pages="broken"),
    {"success": True, "code": 0, "result": {"pages": 1, "count": 1, "data": {}}},
])
def test_incomplete_or_invalid_report_never_returns_partial_evidence(source, payload):
    payloads, _ = source
    payloads[HALT_REPORT] = payload
    with pytest.raises(trading_status.TradingStatusError):
        trading_status.get_daily_trading_status(DAY, ["000016.SZ"])


def test_transport_failure_is_a_status_error(source):
    payloads, _ = source
    payloads[HALT_REPORT] = httpx.ConnectError("上游不可达")
    with pytest.raises(trading_status.TradingStatusError):
        trading_status.get_daily_trading_status(DAY, ["000016.SZ"])


def test_cancel_before_request_prevents_network(source):
    _, requests = source

    def check_cancel():
        raise RuntimeError("用户取消")

    with pytest.raises(RuntimeError, match="用户取消"):
        trading_status.get_daily_trading_status(DAY, ["000016.SZ"], check_cancel=check_cancel)
    assert requests == []


def test_cancel_after_response_does_not_publish_evidence_or_request_next_report(source):
    payloads, requests = source
    returned = False

    def first_report(_):
        nonlocal returned
        returned = True
        return envelope([halt_row()])

    def check_cancel():
        if returned:
            raise RuntimeError("用户取消")

    payloads[HALT_REPORT] = first_report
    with pytest.raises(RuntimeError, match="用户取消"):
        trading_status.get_daily_trading_status(DAY, ["000016.SZ"], check_cancel=check_cancel)
    assert len(requests) == 1
