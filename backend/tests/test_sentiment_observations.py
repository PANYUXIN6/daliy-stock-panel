from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import regime_builder as rb
from app.services import sentiment_observations as so


def observation(row, key):
    return next(o for o in row["sentiment"]["observations"] if o["key"] == key)


def test_fixed_window_no_future_and_revision_cache():
    rows = [dict(date=date(2020, 1, 1) + timedelta(days=i), method_version=so.VERSION,
                 exclude_st=True, is_complete_day=True, total_count=10, up_pct=i) for i in range(510)]
    result = so.observations_series(rows)
    assert observation(result[499], "up_pct")["percentile"] is None
    assert observation(result[500], "up_pct")["percentile"] == 100
    assert observation(result[509], "up_pct")["extreme_streak"] == 10
    assert observation(result[509], "up_pct")["change_5d"] == 5
    assert so.observations_series(rows[:505])[-1] == result[504]
    rows[509]["up_pct"] = 0
    assert observation(so.observations_series(rows)[-1], "up_pct")["percentile"] == 0


def test_intraday_not_ranked_or_added_to_history(monkeypatch):
    monkeypatch.setattr(so, "cn_now", lambda: datetime(2022, 1, 1, 10))
    rows = [dict(date=date(2022, 1, 1)-timedelta(days=500-i), method_version=so.VERSION,
                 is_complete_day=True, up_pct=i) for i in range(501)]
    last = so.observations_series(rows)[-1]
    assert last["sentiment"]["coverage"]["is_complete_day"] is False
    assert observation(last, "up_pct")["percentile"] is None


def test_zero_missing_denominators_and_amount():
    d = date(2020, 1, 1)
    df = pl.DataFrame(dict(date=[d]*3, symbol=["a", "b", "halt"], volume=[1, 1, 0],
        change_pct=[-.1, None, 0], close=[5., 5., 5.], ma20=[10., None, 1.],
        amount=[100., None, 0.], signal_limit_up=[False]*3,
        signal_limit_down=[False]*3, signal_broken_limit_up=[True, False, False],
        consecutive_limit_ups=[0]*3))
    row = rb._aggregate_daily(df, {d: 0}).to_dicts()[0]
    assert row["total_count"] == 2
    assert row["change_valid_count"] == 1
    assert row["up_pct"] == 0
    assert row["strong_down_pct"] == 100
    assert row["above_ma20_pct"] == 0
    assert row["seal_rate"] == 0
    assert row["total_amount"] is None
    missing = rb._aggregate_daily(df.drop("signal_limit_up", "signal_broken_limit_up", "consecutive_limit_ups")).to_dicts()[0]
    assert missing["seal_rate"] is None
    assert missing["limit_up"] is None
    assert missing["ge2_count"] is None
    assert rb.classify_state({}) == ("unavailable", None)
    assert rb._compute_subscores({"index_pct": 0, "above_ma20_pct": 0})["trend"] < rb._compute_subscores({"index_pct": 0, "above_ma20_pct": .5})["trend"]


def test_old_method_and_st_change_not_ranked():
    rows = [dict(date=date(2020, 1, 1)+timedelta(days=i), method_version=so.VERSION,
                 exclude_st=True, is_complete_day=True, up_pct=0) for i in range(501)]
    rows[-1]["exclude_st"] = False
    last = so.observations_series(rows)[-1]
    assert observation(last, "up_pct")["percentile"] is None
    assert observation(last, "up_pct")["history_sample_count"] == 0
    del rows[-1]["method_version"]
    assert so.observations_series(rows)[-1]["sentiment"]["coverage"]["method_current"] is False


def test_overview_uses_identical_persisted_statistics(tmp_path, monkeypatch):
    from app.services import market_overview_builder as overview
    from app.services import preferences
    d = date(2020, 1, 1)
    raw = pl.DataFrame(dict(date=[d], symbol=["a"], close=[10.], volume=[1], change_pct=[.1], amount=[100.], ma20=[9.]))
    daily = rb._aggregate_daily(raw, {d: .01}).with_columns(pl.lit(True).alias("exclude_st"))
    rb.upsert_regime_history(tmp_path, daily)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(overview, "ScreenerService", lambda _: SimpleNamespace(latest_date=lambda: d, _load_enriched_for_date=lambda _: raw))
    monkeypatch.setattr(overview, "_index_quotes", lambda *a: [])
    monkeypatch.setattr(overview, "_dimension_rank", lambda *a, **kw: {"leading": [], "lagging": []})
    monkeypatch.setattr(preferences, "get_sentiment_exclude_st", lambda: True)
    result = overview.build_market_overview(repo)
    assert result["sentiment"] == so.observations_series(daily.to_dicts())[-1]["sentiment"]
    assert result["emotion"]["score"] is None


def test_history_api_ranks_before_query_and_file_cache_invalidates(tmp_path):
    from app.api.regime import regime_history, regime_latest
    rows = [dict(date=date(2020, 1, 1)+timedelta(days=i), method_version=so.VERSION,
                 exclude_st=True, is_complete_day=True, up_pct=i) for i in range(501)]
    rb.upsert_regime_history(tmp_path, pl.DataFrame(rows))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))))
    day = rows[-1]["date"]
    result = regime_history(request, start=day, end=day, limit=120)["rows"][0]
    assert observation(result, "up_pct")["percentile"] == 100
    assert result["sentiment"] == regime_latest(request)["row"]["sentiment"]
    rows[-1]["up_pct"] = -1
    rb.upsert_regime_history(tmp_path, pl.DataFrame([rows[-1]]))
    result = regime_history(request, start=day, end=day, limit=120)["rows"][0]
    assert observation(result, "up_pct")["percentile"] == 0


def test_midrank_constant_and_discrete_series():
    rows = [dict(date=date(2020, 1, 1) + timedelta(days=i), method_version=so.VERSION,
                 exclude_st=True, is_complete_day=True, limit_down=0) for i in range(501)]
    obs = observation(so.observations_series(rows)[-1], "limit_down")
    assert obs["percentile"] == 50
    assert obs["extreme_streak"] == 0
    for row in rows[:250]:
        row["limit_down"] = 1
    assert observation(so.observations_series(rows)[-1], "limit_down")["percentile"] == 25


def test_invalid_volume_and_partial_signal_values_are_not_zero():
    d = date(2020, 1, 1)
    raw = pl.DataFrame({"date": [d] * 4, "volume": [None, float("nan"), -1., 0.],
                        "change_pct": [0.] * 4, "signal_limit_up": [None, False, False, False],
                        "signal_limit_down": [None] * 4,
                        "signal_broken_limit_up": [None, False, False, False]})
    result = rb._aggregate_daily(raw).to_dicts()[0]
    assert result["total_count"] == 3
    for key in ("limit_up", "limit_down", "broken_limit", "touched_limit", "seal_rate"):
        assert result[key] is None


def test_missing_breadth_has_no_directional_context():
    row = dict(date="2020-01-01", method_version=so.VERSION, up_count=0, down_count=0)
    assert so.observations_series([row])[0]["sentiment"]["context"] == []


def test_selected_cache_isolated_and_no_whole_history_serialization(tmp_path, monkeypatch):
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_sentiment_exclude_st", lambda: True)
    rows = [dict(date=date(2020, 1, 1) + timedelta(days=i), method_version=so.VERSION,
                 exclude_st=True, is_complete_day=True, up_pct=i) for i in range(501)]
    rb.upsert_regime_history(tmp_path, pl.DataFrame(rows))
    target = str(rows[-1]["date"])
    original = so.load_observation(tmp_path, target)
    original["observations"][0]["value"] = -999
    assert so.load_observation(tmp_path, target)["observations"][0]["value"] == 500
    monkeypatch.setattr(so.json, "dumps", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("serialized whole history")))
    assert so.load_observation(tmp_path, target)["as_of"] == target
    monkeypatch.setattr(preferences, "get_sentiment_exclude_st", lambda: False)
    assert so.load_observation(tmp_path, target)["coverage"]["method_current"] is False
    assert so.load_observation(tmp_path / "other", target) is None


def test_history_only_last_observation_and_api_nan_safe(tmp_path, monkeypatch):
    from app.api.regime import regime_history, regime_observations
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_sentiment_exclude_st", lambda: True)
    rows = [dict(date=date(2020, 1, 1) + timedelta(days=i), method_version=so.VERSION,
                 exclude_st=True, is_complete_day=True, up_pct=float("nan")) for i in range(2)]
    rb.upsert_regime_history(tmp_path, pl.DataFrame(rows))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))))
    result = regime_history(request, limit=120)["rows"]
    assert "sentiment" not in result[0]
    assert result[0]["up_pct"] is None
    assert result[-1]["sentiment"] == regime_observations(request, rows[-1]["date"])["sentiment"]
    assert regime_observations(request, date(1999, 1, 1))["sentiment"] is None


def test_amount_window_excludes_unclosed_history():
    rows = [dict(date=date(2020, 1, 1) + timedelta(days=i), method_version=so.VERSION,
                 exclude_st=True, is_complete_day=True, total_amount=100.) for i in range(20)]
    assert observation(so.observations_series(rows)[-1], "amount_ratio_5_20")["value"] == 1
    rows[3]["is_complete_day"] = False
    missing = observation(so.observations_series(rows)[-1], "amount_ratio_5_20")
    assert missing["value"] is None
    assert "当前有效19/20日" in missing["missing_reason"]
    assert "2020-01-04收盘口径未确认" in missing["missing_reason"]
    assert missing["percentile_reason"] == missing["missing_reason"]
    # Replacing the source with a confirmed close restores the ratio, while
    # its historical percentile still needs the separate 500-day window.
    rows[3]["is_complete_day"] = True
    restored = observation(so.observations_series(rows)[-1], "amount_ratio_5_20")
    assert restored["value"] == 1
    assert restored["missing_reason"] is None
    assert "当前有效0/500日" in restored["percentile_reason"]


@pytest.mark.parametrize(("change", "reason"), [
    ({"method_version": "old"}, "2020-01-04口径不一致"),
    ({"exclude_st": False}, "2020-01-04口径不一致"),
    ({"total_amount": None}, "2020-01-04成交额缺失"),
])
def test_amount_gap_explains_the_required_repair(change, reason):
    rows = [dict(date=date(2020, 1, 1) + timedelta(days=i), method_version=so.VERSION,
                 exclude_st=True, is_complete_day=True, total_amount=100.) for i in range(20)]
    rows[3].update(change)
    obs = observation(so.observations_series(rows)[-1], "amount_ratio_5_20")
    assert obs["value"] is None
    assert "当前有效19/20日" in obs["missing_reason"]
    assert reason in obs["missing_reason"]


def test_changed_st_marks_raw_history_and_recompute_stale(tmp_path, monkeypatch):
    from app.api.regime import regime_history, regime_latest
    from app.services import preferences
    day = date(2020, 1, 1)
    rb.upsert_regime_history(tmp_path, pl.DataFrame([dict(date=day, method_version=so.VERSION,
        exclude_st=True, is_complete_day=True, score=70., state="strong")]))
    (tmp_path / "kline_daily_enriched").mkdir()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))
    monkeypatch.setattr(preferences, "get_sentiment_exclude_st", lambda: True)
    assert regime_history(request, limit=120)["rows"][0]["method_current"] is True
    assert rb.detect_stale_dates(tmp_path, repo) == []
    monkeypatch.setattr(preferences, "get_sentiment_exclude_st", lambda: False)
    assert regime_history(request, limit=120)["rows"][0]["method_current"] is False
    assert regime_latest(request)["row"]["method_current"] is False
    assert rb.detect_stale_dates(tmp_path, repo) == [day]


def test_atomic_replacement_same_size_and_mtime_invalidates(tmp_path, monkeypatch):
    import os

    from app.services import preferences
    monkeypatch.setattr(preferences, "get_sentiment_exclude_st", lambda: True)
    row = dict(date=date(2020, 1, 1), method_version=so.VERSION, exclude_st=True, is_complete_day=True, up_pct=1.)
    frame = pl.DataFrame([row])
    rb.upsert_regime_history(tmp_path, frame)
    path = rb.regime_path(tmp_path)
    assert so.load_observation(tmp_path)["observations"][0]["value"] == 1
    stat = path.stat()
    replacement = path.with_suffix(".replacement")
    frame.with_columns(pl.lit(2.).alias("up_pct")).write_parquet(replacement)
    assert replacement.stat().st_size == stat.st_size
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replacement.replace(path)
    assert so.load_observation(tmp_path)["observations"][0]["value"] == 2


def test_phase_averages_preserve_missing_and_valid_zero(tmp_path):
    from app.api.regime import regime_phases
    rb.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2020, 1, 1), date(2020, 1, 2)], "phase": ["unknown", "unknown"],
        "max_consecutive": [None, None], "seal_rate": [None, None],
        "first_board": [None, 0], "ge2_count": [None, 4],
    }))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))))
    result = regime_phases(request)["segments"][0]
    assert result["avg_height"] is None
    assert result["avg_seal_rate"] is None
    assert result["avg_first_board"] == 0
    assert result["avg_ge2"] == 4


def test_ma_denominators_exclude_nonpositive_close():
    day = date(2020, 1, 1)
    raw = pl.DataFrame({"date": [day] * 4, "change_pct": [0.] * 4,
                        "close": [0., -1., 5., 15.], "ma20": [10.] * 4, "ma60": [10.] * 4})
    result = rb._aggregate_daily(raw).to_dicts()[0]
    assert result["total_count"] == 4
    for ma in ("ma20", "ma60"):
        assert result[f"{ma}_valid_count"] == 2
        assert result[f"above_{ma}_pct"] == .5
    missing = rb._aggregate_daily(raw.head(2)).to_dicts()[0]
    assert missing["ma20_valid_count"] == 0
    assert missing["above_ma20_pct"] is None
    assert missing["ma60_valid_count"] == 0
    assert missing["above_ma60_pct"] is None


def test_overview_preserves_unknown_volume_in_all_row_consumers(tmp_path, monkeypatch):
    from app.services import market_overview_builder as overview
    from app.services import preferences
    day = date(2020, 1, 1)
    raw = pl.DataFrame({"date": [day] * 3, "symbol": ["unknown", "invalid", "halt"],
        "close": [10.] * 3, "volume": [None, float("nan"), 0.],
        "change_pct": [0., None, 0.], "amount": [100., 200., 0.]})
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(overview, "ScreenerService", lambda _: SimpleNamespace(latest_date=lambda: day, _load_enriched_for_date=lambda _: raw))
    monkeypatch.setattr(overview, "_index_quotes", lambda *a: [])
    seen = []

    def dimension(rows, *args, **kwargs):
        seen.append({row["symbol"] for row in rows})
        return {"leading": [], "lagging": []}

    monkeypatch.setattr(overview, "_dimension_rank", dimension)
    monkeypatch.setattr(preferences, "get_sentiment_exclude_st", lambda: False)
    result = overview.build_market_overview(repo)
    assert result["breadth"]["total"] == 2
    assert seen == [{"unknown", "invalid"}] * 2
    assert {r["symbol"] for r in result["turnover_leaders"]} == {"unknown", "invalid"}


@pytest.mark.parametrize("persisted", [False, True])
@pytest.mark.parametrize("sealed_ready", [False, True])
def test_overview_preserves_depth_corrections(tmp_path, monkeypatch, persisted, sealed_ready):
    from app.services import market_overview_builder as overview
    from app.services import preferences

    day = date(2020, 1, 1)
    raw = pl.DataFrame({
        "date": [day] * 3, "symbol": ["up", "down", "broken"],
        "close": [11., 9., 10.5], "volume": [100] * 3,
        "change_pct": [.1, -.1, .05], "amount": [100.] * 3,
        "signal_limit_up": [True, False, False],
        "signal_limit_down": [False, True, False],
        "signal_broken_limit_up": [False, False, True],
        "consecutive_limit_ups": [1, 0, 0],
    })
    if persisted:
        daily = rb._aggregate_daily(raw).with_columns(pl.lit(False).alias("exclude_st"))
        rb.upsert_regime_history(tmp_path, daily)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    depth = SimpleNamespace(
        get_sealed_map=lambda _day, is_down: {"down" if is_down else "up": {"sealed": False}},
        is_sealed_ready=lambda _day: sealed_ready,
    )
    monkeypatch.setattr(overview, "ScreenerService", lambda _: SimpleNamespace(
        latest_date=lambda: day, _load_enriched_for_date=lambda _: raw))
    monkeypatch.setattr(overview, "_index_quotes", lambda *a: [])
    monkeypatch.setattr(overview, "_dimension_rank", lambda *a, **kw: {"leading": [], "lagging": []})
    monkeypatch.setattr(preferences, "get_sentiment_exclude_st", lambda: False)

    result = overview.build_market_overview(repo, depth_service=depth)
    assert result["limit"]["sealed_ready"] is sealed_ready
    assert result["limit"]["limit_up"] == (0 if sealed_ready else 1)
    assert result["limit"]["limit_down"] == (0 if sealed_ready else 1)
    assert result["limit"]["seal_rate"] == (0 if sealed_ready else 50)
    # The descriptive daily series keeps its daily-signal basis.
    assert next(o for o in result["sentiment"]["observations"] if o["key"] == "limit_up")["value"] == 1


def test_intraday_recompute_never_matures_without_source_refresh(tmp_path, monkeypatch):
    from app.api import regime
    from app.market_time import CN_TZ
    from app.services import market_mainline, preferences

    day = date(2026, 10, 16)
    clock = SimpleNamespace(now=datetime(2026, 10, 16, 10, tzinfo=CN_TZ))
    monkeypatch.setattr(rb, "cn_now", lambda: clock.now, raising=False)
    monkeypatch.setattr(so, "cn_now", lambda: clock.now)
    monkeypatch.setattr(preferences, "get_sentiment_exclude_st", lambda: False)
    monkeypatch.setattr(market_mainline, "compute_mainline_range", lambda *a, **kw: pl.DataFrame())
    history = [dict(date=day - timedelta(days=500-i), method_version=so.VERSION,
                    exclude_st=False, is_complete_day=True, up_pct=0., total_amount=100.)
               for i in range(500)]
    rb.upsert_regime_history(tmp_path, pl.DataFrame(history))
    source = SimpleNamespace(frame=pl.DataFrame({
        "date": [day], "symbol": ["stock"], "volume": [100],
        "close": [11.], "change_pct": [.1], "amount": [100.],
        "quote_ts": [int(clock.now.timestamp() * 1000)],
    }))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
        get_enriched_range=lambda *_: source.frame, get_index_daily=lambda *a, **kw: pl.DataFrame())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))
    assert regime.regime_recompute(request, start=day, end=day)["computed"] == 1
    saved = rb.load_regime_history(tmp_path).filter(pl.col("date") == day).to_dicts()[0]
    assert saved.get("is_complete_day") is False

    for now in (datetime(2026, 10, 16, 15, 1, tzinfo=CN_TZ), datetime(2026, 10, 17, 10, tzinfo=CN_TZ)):
        clock.now = now
        result = so.load_observation(tmp_path, str(day))
        assert result["coverage"]["is_complete_day"] is False
        assert next(o for o in result["observations"] if o["key"] == "up_pct")["percentile"] is None

    next_day = {**history[-1], "date": day + timedelta(days=1), "up_pct": 100.}
    rb.upsert_regime_history(tmp_path, pl.DataFrame([next_day]))
    clock.now = datetime(2026, 10, 17, 16, tzinfo=CN_TZ)
    later = so.load_observation(tmp_path, str(next_day["date"]))
    breadth = next(o for o in later["observations"] if o["key"] == "up_pct")
    assert breadth["history_sample_count"] == 499
    assert breadth["percentile"] is None
    assert next(o for o in later["observations"] if o["key"] == "amount_ratio_5_20")["value"] is None
    # Recomputing the same stale source after the close cannot certify it.
    assert regime.regime_recompute(request, start=day, end=day)["computed"] == 1
    assert so.load_observation(tmp_path, str(day))["coverage"]["is_complete_day"] is False

    # A successful refresh from authoritative daily data replaces the flag.
    source.frame = source.frame.with_columns(pl.lit(None, dtype=pl.Int64).alias("quote_ts"))
    assert regime.regime_recompute(request, start=day, end=day)["computed"] == 1
    refreshed = so.load_observation(tmp_path, str(day))
    assert refreshed["coverage"]["is_complete_day"] is True
    assert next(o for o in refreshed["observations"] if o["key"] == "up_pct")["percentile"] == 100


@pytest.mark.parametrize("complete", [None, False])
def test_unknown_or_unclosed_record_is_not_a_rank_sample(complete):
    day = date(2020, 1, 1)
    rows = [dict(date=day + timedelta(days=i), method_version=so.VERSION,
                 exclude_st=True, is_complete_day=True, up_pct=0.) for i in range(502)]
    rows[500]["is_complete_day"] = complete
    rows[500]["up_pct"] = 100.
    result = so.observations_series(rows)
    assert result[500]["sentiment"]["coverage"]["is_complete_day"] is False
    assert observation(result[500], "up_pct")["percentile"] is None
    assert observation(result[501], "up_pct")["history_sample_count"] == 499
    del rows[500]["is_complete_day"]
    assert so.observations_series(rows)[500]["sentiment"]["coverage"]["is_complete_day"] is False


@pytest.mark.parametrize("snapshot_hour,newer_formal_day", [
    (10, False), (10, True), (15, False), (None, False),
], ids=["intraday-latest", "intraday-before-legacy-latest", "closing-snapshot", "legacy-formal"])
def test_real_repository_warmup_preserves_observation_source(
    tmp_path, monkeypatch, snapshot_hour, newer_formal_day,
):
    from app.api import regime
    from app.market_time import CN_TZ
    from app.services import market_mainline, preferences, shared_runtime
    from app.tickflow.repository import DataStore, KlineRepository

    day = date(2026, 10, 16)
    latest = day + timedelta(days=3) if newer_formal_day else day
    now = datetime.combine(latest, datetime.min.time(), tzinfo=CN_TZ).replace(hour=16)
    monkeypatch.setattr(rb, "cn_now", lambda: now)
    monkeypatch.setattr(so, "cn_now", lambda: now)
    monkeypatch.setattr(preferences, "get_sentiment_exclude_st", lambda: False)
    monkeypatch.setattr(market_mainline, "compute_mainline_range", lambda *a, **kw: pl.DataFrame())
    history = [dict(date=day - timedelta(days=500-i), method_version=so.VERSION,
                    exclude_st=False, is_complete_day=True, up_pct=0.) for i in range(500)]
    rb.upsert_regime_history(tmp_path, pl.DataFrame(history))

    def bar(target, close=10.):
        return pl.DataFrame({"symbol": ["600000.SH"], "date": [target],
            "open": [10.], "high": [11.], "low": [9.], "close": [close],
            "raw_close": [close], "raw_high": [11.], "raw_low": [9.],
            "volume": [100.], "amount": [1000.]})

    source = bar(day, close=11.)
    timestamp = None
    if snapshot_hour is not None:
        timestamp = int(datetime(2026, 10, 16, snapshot_hour, tzinfo=CN_TZ).timestamp() * 1000)
        source = source.with_columns(pl.lit(timestamp).alias("quote_ts"))
    writer = KlineRepository(DataStore(tmp_path))
    try:
        previous = pl.concat([bar(day - timedelta(days=i)) for i in range(1, 66)])
        writer.append_enriched(previous)
        writer.flush_live_enriched_asset("stock", source)
        if newer_formal_day:
            writer.append_enriched(bar(latest))  # Latest schema has no quote_ts.
    finally:
        writer.db.close()

    repo = KlineRepository(DataStore(tmp_path))
    peer = KlineRepository(DataStore(tmp_path))
    try:
        # Execute the production startup path, with no repository/cache fakes.
        repo.refresh_cache(background=True)
        repo._warmup_thread.join(timeout=15)
        assert not repo._warmup_thread.is_alive()
        cached = repo.get_enriched_range(day, day)
        assert cached is not None and not cached.is_empty()  # Must hit the warmed cache.
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))
        assert regime.regime_recompute(request, start=day, end=day)["computed"] == 1
        result = so.load_observation(tmp_path, str(day))
        breadth = next(o for o in result["observations"] if o["key"] == "up_pct")
        closed = snapshot_hour is None or snapshot_hour >= 15
        assert breadth["percentile"] == (100 if closed else None)
        assert result["coverage"]["is_complete_day"] is closed
        assert cached["quote_ts"].to_list() == [timestamp]
        stored = pl.read_parquet(tmp_path / "kline_daily_enriched" / f"date={day}" / "part.parquet")
        if timestamp is not None:
            assert stored["quote_ts"].to_list() == [timestamp]

        # Published latest-day snapshots must retain the same provenance.
        qs = SimpleNamespace(_repo=repo, status=lambda: {}, get_index_quotes=lambda: pl.DataFrame())
        shared_runtime.publish_quotes(qs)
        state = SimpleNamespace(repo=peer, quote_service=shared_runtime.QuoteMirror(peer))
        assert state.quote_service.load_snapshot(state)
        mirrored, mirrored_day = peer.get_enriched_latest(refresh=False)
        assert mirrored_day == latest
        assert mirrored["quote_ts"].to_list() == [None if newer_formal_day else timestamp]

        if snapshot_hour == 10 and not newer_formal_day:
            # A formal source replacement, followed by the same real refresh,
            # is sufficient to recover the row's eligibility.
            repo.flush_live_enriched_asset("stock", source.with_columns(
                pl.lit(None, dtype=pl.Int64).alias("quote_ts")))
            repo.refresh_cache()
            assert regime.regime_recompute(request, start=day, end=day)["computed"] == 1
            refreshed = so.load_observation(tmp_path, str(day))
            assert refreshed["coverage"]["is_complete_day"] is True
            assert next(o for o in refreshed["observations"] if o["key"] == "up_pct")["percentile"] == 100
    finally:
        if repo._warmup_thread is not None:
            repo._warmup_thread.join(timeout=15)
        repo.db.close()
        peer.db.close()
