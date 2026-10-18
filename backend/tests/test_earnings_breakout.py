import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import kline
from app.indicators.earnings_breakout import compute_earnings_breakout
from app.services import earnings_breakout as service
from app.services.financial_versions import read_versions, record_versions


def bars(kind="9", year=2026):
    start = date(year, 1, 1)
    rows = [dict(symbol="600000.SH", date=start + timedelta(days=i),
                 open=10.0, high=10.2, low=9.8, close=10.0) for i in range(40)]
    rows.append(dict(symbol="600000.SH", date=start + timedelta(days=40),
                     open=11.1, high=12.0, low=11.0, close=11.5))
    for i in range(1, 15):
        low = 11.05 if kind == "9" else 11.6
        close = (11.3 if i % 2 else 11.4) if kind == "9" else (11.8 if i % 2 else 11.9)
        rows.append(dict(symbol="600000.SH", date=start + timedelta(days=40 + i),
                         open=low, high=max(close, low) + .1, low=low, close=close))
    return pl.DataFrame(rows)


def calendar(prices):
    return {"start": str(prices["date"][0]), "end": str(prices["date"][-1]),
            "days": [str(d) for d in prices["date"]]}


def financial_row(period, available, profit):
    return {"symbol": "600000.SH", "period_end": period, "net_income": profit,
            "announce_date": available, "source_update_date": available,
            "date_basis": "source_update", "disclosure_type": "formal"}


def baseline(tmp_path, year=2026, missing_prior=False):
    current = financial_row(f"{year-1}-12-31", f"{year}-02-09", 100.)
    rows = [current] if missing_prior else [financial_row(f"{year-2}-12-31", f"{year-1}-03-01", 50.), current]
    return record_versions(tmp_path, "income", pl.DataFrame(rows), "fuyao",
                           datetime.fromisoformat(f"{year}-02-09T20:00:00+08:00"))


@pytest.mark.parametrize("year", [2022, 2026])
def test_icon_9_gap_pullback_without_date_cutoff(year):
    frame = bars(year=year)
    event = frame["date"][40]
    out = compute_earnings_breakout(frame, {event})
    assert out["earnings_gap"][40]
    assert out["signal_earnings_9"][42]
    assert out["earnings_9_event_date"][42] == event
    assert not out["signal_earnings_9"][:42].any()
    assert not out["signal_earnings_42"].any()
    broken = frame.with_columns(pl.when(pl.col("date") == frame["date"][43])
                                .then(10.2).otherwise(pl.col("low")).alias("low"))
    assert not compute_earnings_breakout(broken, {event})["signal_earnings_9"][43:].any()


def test_icon_42_midpoint_holds_short_window_and_future_prefix():
    frame = bars("42")
    event = frame["date"][40]
    out = compute_earnings_breakout(frame, {event})
    assert not out["signal_earnings_42"][:45].any()
    assert out["signal_earnings_42"][45]
    assert out["earnings_42_event_date"][45] == event
    assert not out["signal_earnings_42"][54]
    prefix = compute_earnings_breakout(frame.head(46), {event})
    assert out.select("signal_earnings_9", "signal_earnings_42").head(46).equals(
        prefix.select("signal_earnings_9", "signal_earnings_42"))
    broken = frame.with_columns(pl.when(pl.col("date") == frame["date"][45])
                                .then(11.49).otherwise(pl.col("low")).alias("low"))
    assert not compute_earnings_breakout(broken, {event})["signal_earnings_42"][45:].any()


def test_icon_9_confirmation_window_expires_at_125_bars():
    frame = bars().head(41)
    event = frame["date"][40]
    extra = [dict(symbol="600000.SH", date=event + timedelta(days=i), open=11.05,
                  high=11.8, low=11.05, close=11.3 if i < 126 else 11.6 + (i - 126) * .1)
             for i in range(1, 128)]
    out = compute_earnings_breakout(pl.concat([frame, pl.DataFrame(extra)]), {event})
    assert out["signal_earnings_9"][166]
    assert not out["signal_earnings_9"][167]


def test_icon_9_three_percent_branch_above_event_high():
    frame = bars()
    frame = frame.with_columns([
        pl.when(pl.col("date") > frame["date"][40]).then(value).otherwise(pl.col(column)).alias(column)
        for column, value in [("low", 12.2), ("open", 12.2), ("high", 12.6), ("close", 12.4)]
    ])
    assert compute_earnings_breakout(frame, {frame["date"][40]})["signal_earnings_9"][42]
    assert frame["low"][42] > frame["low"][40] * 1.05


def test_revisions_of_current_or_comparative_never_erase_or_create_old_event(tmp_path):
    original = baseline(tmp_path)
    prices = bars("42")
    before, _ = service.report_events(original, calendar(prices), prices["date"][-1])
    assert before[0]["date"] == str(prices["date"][40])
    assert before[0]["net_income_yoy"] == 100.
    # 当前期改成亏损，比较期大幅上修，原断层仍对应初始两个版本。
    revisions = pl.DataFrame([
        financial_row("2025-12-31", "2026-02-16", -100.),
        financial_row("2024-12-31", "2026-02-16", 500.),
    ])
    record_versions(tmp_path, "income", revisions, "fuyao", datetime.fromisoformat("2026-02-16T20:00:00+08:00"))
    after, _ = service.report_events(read_versions(tmp_path, "income"), calendar(prices), prices["date"][-1])
    assert after == before
    assert compute_earnings_breakout(prices, {date.fromisoformat(e["date"]) for e in after})["signal_earnings_42"][45]
    # 原先不满足业绩条件，后续正向修订也不能回补原缺口。
    other = tmp_path / "other"
    base = original.drop("observed_at", "available_date", "availability_basis").with_columns(
        pl.when(pl.col("period_end") == "2025-12-31").then(-100.).otherwise(pl.col("net_income")).alias("net_income"))
    record_versions(other, "income", base, "fuyao", datetime.fromisoformat("2026-02-09T20:00:00+08:00"))
    record_versions(other, "income", pl.DataFrame([financial_row("2025-12-31", "2026-02-16", 100.)]), "fuyao",
                    datetime.fromisoformat("2026-02-16T20:00:00+08:00"))
    assert service.report_events(read_versions(other, "income"), calendar(prices), prices["date"][-1])[0] == []


def test_missing_contemporaneous_comparative_or_late_version_cannot_backfill(tmp_path):
    prices = bars()
    baseline(tmp_path, missing_prior=True)
    # 比较期后来才观察到，不得把它挪到原缺口日。
    record_versions(tmp_path, "income", pl.DataFrame([financial_row("2024-12-31", "2025-03-01", 50.)]), "fuyao",
                    datetime.fromisoformat("2026-02-16T20:00:00+08:00"))
    events, _ = service.report_events(read_versions(tmp_path, "income"), calendar(prices), prices["date"][-1])
    assert events == []
    late = tmp_path / "late"
    baseline(late)
    row = pl.DataFrame([financial_row("2025-12-31", "2026-03-01", 100.)])
    only_revision = record_versions(tmp_path / "only_revision", "income", row, "fuyao",
                                   datetime.fromisoformat("2026-03-01T20:00:00+08:00"))
    assert service.report_events(only_revision, calendar(prices), prices["date"][-1])[0] == []


def test_calendar_weekend_halt_and_legacy_missing_evidence(tmp_path):
    data = baseline(tmp_path)
    cal = {"start": "2026-01-01", "end": "2026-02-20", "days": ["2026-02-09", "2026-02-12"]}
    events, _ = service.report_events(data, cal, date(2026, 2, 20))
    assert events[0]["date"] == "2026-02-12"  # 日历明确休市，不能用自然日或单股复牌日。
    assert service.report_events(data.drop("observed_at"), cal, date(2026, 2, 20))[0] == []
    frame = bars().filter(pl.col("date") != date(2026, 2, 12))
    assert not compute_earnings_breakout(frame, {date(2026, 2, 12)})["earnings_gap"].any()
    assert not compute_earnings_breakout(bars(), set())["signal_earnings_9"].any()


def test_api_local_read_visible_range_live_update_and_unsupported(tmp_path, monkeypatch):
    baseline(tmp_path)
    prices = bars("42")
    path = tmp_path / "calendar"
    path.mkdir()
    (path / "trading_days.json").write_text(json.dumps(calendar(prices)))
    monkeypatch.setattr(service.preferences, "get_financial_provider", lambda: "fuyao")
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda symbol: "etf" if symbol == "510300.SH" else "stock",
        get_financial_versions=lambda table, symbol, columns: read_versions(tmp_path, table, columns, symbol=symbol),
        get_daily_asset=lambda *a, **k: prices)
    app = FastAPI()
    app.include_router(kline.router)
    app.state.repo = repo
    client = TestClient(app)
    params = {"symbol": "600000.SH", "start_date": str(prices["date"][45]), "end_date": str(prices["date"][-1])}
    payload = client.get("/api/kline/earnings-breakout", params=params).json()
    assert payload["status"] == "ready" and payload["events"] == []  # 锚点在可见区间前。
    assert payload["markers"][0] == {"date": str(prices["date"][45]), "icon": 42, "event_date": str(prices["date"][40])}
    assert client.get("/api/kline/earnings-breakout", params={**params, "symbol": "000001.SZ"}).json()["status"] == "no_versions"
    assert client.get("/api/kline/earnings-breakout", params={**params, "symbol": "510300.SH"}).json()["status"] == "unsupported"
    assert client.get("/api/kline/earnings-breakout", params={**params, "symbol": "../x"}).status_code == 422
    assert client.get("/api/kline/earnings-breakout", params={**params, "start_date": "2026-12-31"}).status_code == 400
    monkeypatch.setattr(kline, "cn_today", lambda: prices["date"][45])
    live = prices.row(45, named=True)
    live["date"] = str(live["date"])
    live["low"] = 11.49
    monkeypatch.setattr(kline, "_latest_live_candle", lambda *a, **k: live)
    repo.get_daily_asset = lambda *a, **k: prices.head(45)
    live_payload = client.get("/api/kline/earnings-breakout", params=params).json()
    assert not any(m["icon"] == 42 for m in live_payload["markers"])
    (tmp_path / "financials/versions/income/part.parquet").write_bytes(b"broken")
    assert client.get("/api/kline/earnings-breakout", params=params).status_code == 503


@pytest.mark.parametrize("profit,previous", [(50., 50.), (40., 50.), (100., 0.), (float('nan'), 50.)])
def test_nonpositive_growth_zero_comparative_and_missing_values_are_not_events(tmp_path, profit, previous):
    frame = baseline(tmp_path).with_columns(
        pl.when(pl.col('period_end') == '2025-12-31').then(profit).otherwise(previous).alias('net_income'))
    prices = bars()
    assert service.report_events(frame, calendar(prices), prices['date'][-1])[0] == []


def test_late_old_period_does_not_replace_new_report_and_sources_never_mix(tmp_path):
    versions = baseline(tmp_path)
    record_versions(tmp_path, 'income', pl.DataFrame([financial_row('2025-09-30', '2026-02-15', 200.)]),
                            'fuyao', datetime.fromisoformat('2026-02-15T20:00:00+08:00'))
    prices = bars()
    assert service.report_events(read_versions(tmp_path, 'income'), calendar(prices), prices['date'][-1])[0][0]['period_end'] == '2025-12-31'
    other_source = versions.with_columns(pl.lit('other').alias('source'))
    with pytest.raises(ValueError, match='不能混合'):
        service.report_events(pl.concat([versions, other_source]), calendar(prices), prices['date'][-1])


def test_calendar_refresh_does_not_discard_formed_event_date(tmp_path, monkeypatch):
    from app.data_providers import custom
    from app.services import trading_day

    versions = baseline(tmp_path)
    old_calendar = calendar(bars())
    folder = tmp_path / 'calendar'
    folder.mkdir()
    (folder / 'trading_days.json').write_text(json.dumps(old_calendar))
    before, _ = service.report_events(versions, old_calendar, date(2026, 3, 2))
    provider = SimpleNamespace(trading_days=lambda: {date(2026, 2, 24), date(2026, 3, 2)})
    monkeypatch.setattr(custom, 'is_custom_provider', lambda name: True)
    monkeypatch.setattr(custom, 'get_provider', lambda name: provider)
    monkeypatch.setattr(trading_day, 'cn_now', lambda: datetime.fromisoformat('2026-03-02T08:00:00+08:00'))
    trading_day.sync_calendar(tmp_path)
    after, _ = service.report_events(versions, trading_day.calendar_status(tmp_path), date(2026, 3, 2))
    assert after == before and after[0]['date'] == '2026-02-10'
