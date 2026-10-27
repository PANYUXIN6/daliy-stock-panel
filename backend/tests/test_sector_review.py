"""Fixed formal inputs, temporal invariants, read-only queries and version conflicts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.sector_review import router
from app.services import sector_review as service
from app.services import sector_review_rules as rules
from app.tickflow.sector_review_reader import SectorReviewReader


@pytest.fixture
def market(tmp_path):
    import json

    days = [
        date(2026, 1, 5) + timedelta(days=i)
        for i in range(130)
        if (date(2026, 1, 5) + timedelta(days=i)).weekday() < 5
    ]
    (tmp_path / "calendar").mkdir()
    (tmp_path / "calendar/trading_days.json").write_text(
        json.dumps(
            {
                "start": days[0].isoformat(),
                "end": days[-1].isoformat(),
                "days": [d.isoformat() for d in days],
            }
        )
    )
    symbols = [f"60000{i}.SH" for i in range(6)]
    (tmp_path / "instruments").mkdir()
    pl.DataFrame(
        {
            "symbol": symbols,
            "name": [f"成员{i}" for i in range(6)],
            "exchange": ["SH"] * 6,
            "type": ["stock"] * 6,
            "listing_date": ["2020-01-01"] * 6,
        }
    ).write_parquet(tmp_path / "instruments/part.parquet")
    base = tmp_path / "ext_data/test_industry"
    base.mkdir(parents=True)
    (base / "config.json").write_text(
        json.dumps(
            {
                "id": "test_industry",
                "label": "测试行业",
                "mode": "snapshot",
                "fields": [{"name": "industry"}],
            }
        )
    )
    pl.DataFrame({"symbol": symbols, "industry": ["一级-二级-三级"] * 6}).write_parquet(
        base / "part.parquet"
    )
    for i, day in enumerate(days):
        # Benchmark declines, sector rises; T (day 80) has expanded amounts.
        prices = [10 * 1.001**i] * 6
        amounts = [1000 + n * 100 for n in range(6)]
        if i >= 80:
            amounts = [a * 2 for a in amounts]
        raw = pl.DataFrame(
            {
                "symbol": symbols,
                "date": [day] * 6,
                "open": prices,
                "high": [p * 1.01 for p in prices],
                "low": [p * 0.99 for p in prices],
                "close": prices,
                "volume": [100.0] * 6,
                "amount": amounts,
            }
        )
        for table in ("kline_daily", "kline_daily_enriched"):
            path = tmp_path / table / f"date={day}/part.parquet"
            path.parent.mkdir(parents=True)
            frame = (
                raw
                if table == "kline_daily"
                else raw.with_columns(pl.col("close").alias("raw_close"))
            )
            frame.write_parquet(path)
        p = 100 * 0.999**i
        index = pl.DataFrame(
            {
                "symbol": list(service.CORE_INDEX_NAMES),
                "date": [day] * 4,
                "open": [p] * 4,
                "high": [p * 1.01] * 4,
                "low": [p * 0.99] * 4,
                "close": [p] * 4,
                "volume": [100.0] * 4,
                "amount": [10000.0 if i < 80 else 20000.0] * 4,
            }
        )
        path = tmp_path / "kline_index_daily" / f"date={day}/part.parquet"
        path.parent.mkdir(parents=True)
        index.write_parquet(path)
    reader = SectorReviewReader(tmp_path)
    repo = SimpleNamespace(sector_review_reader=lambda: reader)
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    yield SimpleNamespace(
        root=tmp_path, reader=reader, repo=repo, days=days, symbols=symbols, client=TestClient(app)
    )


@pytest.mark.parametrize("mode", ["event", "countertrend"])
@pytest.mark.parametrize("missing_offset", [0, -1])
def test_missing_benchmark_amount_is_partial_without_discarding_price(market, mode, missing_offset):
    day = market.days[80]
    path = market.root / f"kline_index_daily/date={market.days[80 + missing_offset]}/part.parquet"
    pl.read_parquet(path).with_columns(
        pl.lit(None, dtype=pl.Float64).alias("amount")
    ).write_parquet(path)
    query = {"event_date": str(day)} if mode == "event" else {"as_of": str(day)}
    response = market.client.get(f"/api/sector-review/{mode}", params=query)
    assert response.status_code == 200, response.text
    result = response.json()
    row = result["rows"][0]
    assert result["benchmark_evidence"]["amount_ratio"] is None
    assert row["latest"]["status"] == "partial"
    assert row["data_complete"] is False
    assert result["status"] == "partial"
    assert result["coverage"]["complete_sectors"] == 0
    assert row["relative_return"] is not None
    if mode == "event":
        assert row["candidate"] is True


@pytest.mark.parametrize("mode", ["event", "countertrend"])
def test_summary_and_members_share_continuous_window_returns(market, mode):
    args = (
        {"mode": mode, "event_date": market.days[80], "view_as_of": market.days[82]}
        if mode == "event"
        else {"mode": mode, "as_of": market.days[82], "window": 5}
    )
    path = market.root / f"kline_daily/date={market.days[81]}/part.parquet"
    original = pl.read_parquet(path)
    original.filter(pl.col("symbol") != market.symbols[0]).write_parquet(path)
    result = service.get_snapshot(market.repo, **args).payload
    row = result["rows"][0]
    detail = service.members(
        market.repo, sector_id=row["sector_id"], snapshot_id=result["snapshot_id"], **args
    )
    valid = [
        r for r in detail["rows"] if r["formation_eligible"] and r["window_return"] is not None
    ]
    assert row["formed_members"] == 6
    assert row["member_return_count"] == len(valid) == 5
    assert row["member_up_ratio"] is None
    assert row["member_median_return"] is None
    original.write_parquet(path)
    restored = service.get_snapshot(market.repo, **args).payload["rows"][0]
    assert restored["member_return_count"] == 6
    assert restored["member_up_ratio"] == 1


@pytest.mark.parametrize("current", [False, None, True])
def test_latest_complete_day_uses_current_universe_and_preserves_unknowns(market, current):
    path = market.root / "instruments/part.parquet"
    frame = pl.read_parquet(path).with_columns(pl.lit(True).alias("is_current"))
    extra = frame.head(1).with_columns(
        pl.lit("600099.SH").alias("symbol"), pl.lit(current, dtype=pl.Boolean).alias("is_current")
    )
    pl.concat([frame, extra]).write_parquet(path)
    assert market.reader.instruments().height == 7
    result = service.availability(market.repo)
    assert result["default_date"] == (str(market.days[-1]) if current is False else None)
    assert result["audit"][0]["expected"] == (6 if current is False else 7)


def test_noncurrent_stock_remains_in_historical_research_members(market):
    path = market.root / "instruments/part.parquet"
    frame = pl.read_parquet(path).with_columns(
        (pl.col("symbol") != market.symbols[0]).alias("is_current")
    )
    frame.write_parquet(path)
    result = service.get_snapshot(market.repo, mode="event", event_date=market.days[80]).payload
    row = result["rows"][0]
    assert row["formed_members"] == 6
    assert result["coverage"]["formed_market_members"] == 6
    detail = service.members(
        market.repo,
        mode="event",
        event_date=market.days[80],
        sector_id=row["sector_id"],
        snapshot_id=result["snapshot_id"],
    )
    assert detail["rows"][0]["formation_eligible"] is True


def test_compound_classification_and_conditional_denominator():
    assert rules.classify(-0.02, -0.05) == "resilient"
    assert rules.classify(0.01, -0.05) == "countertrend_up"
    assert rules.classify(0.01, 0.005) == "relative_leader"
    assert rules.classify(0.01, 0) == "benchmark_flat"
    stats = rules.down_statistics(
        [0.01, 0.01, 0.01, -0.03] + [0.02] * 16, [-0.01] * 4 + [0.01] * 16
    )
    assert (
        stats["wins"],
        stats["comparable_days"],
        stats["benchmark_down_days"],
        stats["ratio"],
    ) == (3, 4, 4, 0.75)
    assert rules.down_statistics([0.01], [0.01])["ratio"] is None
    assert rules.down_statistics([0.01, 0.01], [-0.01, None])["benchmark_down_days"] is None
    assert not rules.down_statistics([None], [-0.01])["complete"]
    assert rules.relative(rules.compound([0.1, -0.1]), rules.compound([0, 0.1])) == pytest.approx(
        0.99 / 1.1 - 1
    )


@pytest.mark.parametrize(
    ("flags", "first", "streak", "complete"),
    [
        ([False, False, True], 2, 1, True),
        ([None, False, True], None, 1, True),
        ([True, None, True], 0, 1, False),
        ([True, None], 0, None, False),
        ([None, False], None, 0, True),
    ],
)
def test_response_path_unknowns(flags, first, streak, complete):
    result = rules.response_path(
        [
            {"date": str(i), "strength": flag, "daily_resonance_candidate": None}
            for i, flag in enumerate(flags)
        ]
    )
    assert result["first_strength_offset"] == first
    assert result["strength_streak"] == streak
    assert result["streak_complete"] is complete


def test_countertrend_event_and_readonly(market):
    before = {
        str(p.relative_to(market.root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in market.root.rglob("*")
        if p.is_file()
    }
    response = market.client.get(
        "/api/sector-review/countertrend", params={"as_of": str(market.days[80])}
    )
    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["classification"] == "countertrend_up"
    assert row["down_days"]["comparable_days"] == 20
    assert row["formed_members"] == 6
    assert row["sector_return"] == pytest.approx(1.001**20 - 1)
    assert row["latest"]["amount_ratio"] == pytest.approx(2)
    assert row["latest"]["breadth_diff"] == pytest.approx(0)
    assert response.json()["etf"]["status"] == "unconfigured"
    event = market.client.get(
        "/api/sector-review/event",
        params={"event_date": str(market.days[80]), "view_as_of": str(market.days[83])},
    )
    assert event.status_code == 200, event.text
    erow = event.json()["rows"][0]
    assert erow["confirmed_first_strength_date"] == str(market.days[80])
    assert erow["strength_streak"] == 4
    assert erow["observations"][3]["amount_ratio"] == pytest.approx(2)
    assert erow["core_members"][0]["symbol"] == market.symbols[-1]
    assert erow["background"]["60"]["sector_return"] == pytest.approx(1.001**60 - 1)
    t_only = market.client.get(
        "/api/sector-review/event", params={"event_date": str(market.days[80])}
    ).json()["rows"][0]
    assert t_only["observations"][0] == erow["observations"][0]
    assert t_only["background"] == erow["background"]
    assert (
        t_only["core_members"][0]["formation_mean_amount"]
        == erow["core_members"][0]["formation_mean_amount"]
    )
    after = {
        str(p.relative_to(market.root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in market.root.rglob("*")
        if p.is_file()
    }
    assert before == after
    assert not list(market.root.glob(".matrix*"))


def test_missing_member_fixed_set_and_snapshot_conflict(market):
    args = {"mode": "event", "event_date": market.days[80], "view_as_of": market.days[82]}
    snapshot = service.get_snapshot(market.repo, **args).payload
    path = market.root / f"kline_daily/date={market.days[81]}/part.parquet"
    pl.read_parquet(path).filter(pl.col("symbol") != market.symbols[0]).write_parquet(path)
    result = service.get_snapshot(market.repo, **args).payload
    row = result["rows"][0]
    assert row["formed_members"] == 6
    assert row["observations"][1]["day_return"] is None
    assert row["observations"][1]["amount"] is None
    assert row["relative_return"] is None
    assert result["snapshot_id"] != snapshot["snapshot_id"]
    with pytest.raises(service.ReviewSnapshotConflictError):
        service.members(
            market.repo, sector_id=row["sector_id"], snapshot_id=snapshot["snapshot_id"], **args
        )
    detail = service.members(
        market.repo, sector_id=row["sector_id"], snapshot_id=result["snapshot_id"], **args
    )
    assert detail["rows"][0]["observations"][1]["reason"] == "missing_quote"
    assert detail["rows"][0]["window_return"] is None


def test_nonformal_snapshots_and_formation_exclusions(market):
    day = market.days[80]
    path = market.root / f"kline_daily/date={day}/part.parquet"
    pl.read_parquet(path).with_columns(pl.lit(1790000000000).alias("quote_ts")).write_parquet(path)
    result = service.get_snapshot(market.repo, mode="event", event_date=day).payload
    row = result["rows"][0]
    assert row["formed_members"] == 6
    assert row["candidate"] is None
    assert row["observations"][0]["day_return"] is None
    assert row["missing_reasons"] == {"non_formal_snapshot": 6}


def test_validation_calendar_and_members_pagination(market):
    client = market.client
    assert client.get("/api/sector-review/countertrend?window=9").status_code == 422
    assert client.get("/api/sector-review/event?event_date=2026-01-10").status_code == 422
    assert (
        client.get(
            "/api/sector-review/event",
            params={"event_date": str(market.days[80]), "view_as_of": str(market.days[84])},
        ).status_code
        == 422
    )
    params = {"event_date": str(market.days[80])}
    data = client.get("/api/sector-review/event", params=params).json()
    detail = client.get(
        "/api/sector-review/members",
        params={
            **params,
            "mode": "event",
            "sector_id": data["rows"][0]["sector_id"],
            "snapshot_id": data["snapshot_id"],
            "page_size": 2,
        },
    )
    assert detail.status_code == 200, detail.text
    assert len(detail.json()["rows"]) == 2
    assert detail.json()["total"] == 6
    assert (
        client.get("/api/sector-review/countertrend?dimension_id=../../etc/passwd").status_code
        == 422
    )


def test_availability_and_optional_etf_failure(market):
    (market.root / ".matrix_generation_etf.json").write_text('{"state":"publishing"}')
    result = service.availability(market.repo)
    assert result["default_date"] == str(market.days[-1])
    assert (
        service.get_snapshot(market.repo, mode="event", event_date=market.days[80]).payload["rows"][
            0
        ]["candidate"]
        is True
    )
    (market.root / ".matrix_generation_stock.json").write_text('{"state":"publishing"}')
    result = market.client.get(
        "/api/sector-review/event", params={"event_date": str(market.days[80])}
    ).json()
    assert result["status"] == "unavailable"


def test_singleflight_and_concurrent_revision(market, monkeypatch):
    original = service._build
    calls = []

    def build(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(service, "_build", build)
    args = {"mode": "event", "event_date": market.days[80]}
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: service.get_snapshot(market.repo, **args), range(4)))
    assert len(calls) == 1
    assert len({r.payload["snapshot_id"] for r in results}) == 1


def test_sector_amount_gap_keeps_independent_price_candidate(market):
    day = market.days[80]
    path = market.root / f"kline_daily/date={day}/part.parquet"
    raw = pl.read_parquet(path)
    raw.with_columns(pl.lit(None, dtype=pl.Float64).alias("volume")).write_parquet(path)
    read = market.reader.prices(day, day, market.reader.instruments())
    assert read["price"].null_count() == 0
    assert not read["formal_complete"].any()
    raw.with_columns(pl.lit(None, dtype=pl.Float64).alias("amount")).write_parquet(path)
    row = service.get_snapshot(market.repo, mode="event", event_date=day).payload["rows"][0]
    assert row["latest"]["day_return"] == pytest.approx(0.001)
    assert row["latest"]["breadth_up"] == 1
    assert row["latest"]["amount"] is None
    assert row["latest"]["amount_ratio"] is None
    assert row["candidate"] is True
    assert not row["data_complete"]
    assert row["missing_reasons"] == {"invalid_amount": 6}


def test_market_gaps_do_not_clear_complete_sector_metrics(market):
    classification = market.root / "ext_data/test_industry/part.parquet"
    pl.read_parquet(classification).filter(pl.col("symbol") != market.symbols[-1]).write_parquet(
        classification
    )
    day = market.days[80]
    path = market.root / f"kline_daily/date={day}/part.parquet"
    pl.read_parquet(path).filter(pl.col("symbol") != market.symbols[-1]).write_parquet(path)
    row = service.get_snapshot(market.repo, mode="event", event_date=day).payload["rows"][0]
    assert row["formed_members"] == 5
    assert row["latest"]["day_return"] == pytest.approx(0.001)
    assert row["latest"]["breadth_up"] == 1
    assert row["latest"]["breadth_diff"] is None
    assert row["latest"]["amount_share"] is None
    assert row["candidate"] is True
    assert not row["data_complete"]


def test_short_background_keeps_price_candidate_and_exposes_sixty_day_gap(market):
    row = service.get_snapshot(market.repo, mode="event", event_date=market.days[25]).payload[
        "rows"
    ][0]
    assert row["formed_members"] == 6
    assert row["candidate"] is True
    assert row["background"]["60"]["sector_return"] is None
    assert not row["data_complete"]


def test_observed_first_date_does_not_fill_missing_days(market):
    day = market.days[80]
    (market.root / f"kline_daily/date={day}/part.parquet").unlink()
    row = service.get_snapshot(
        market.repo, mode="event", event_date=day, view_as_of=market.days[82]
    ).payload["rows"][0]
    assert row["earliest_observed_strength_date"] == str(market.days[82])
    assert row["confirmed_first_strength_date"] is None
    assert row["strength_streak"] == 1
    assert row["streak_complete"] is False


def test_publication_revision_during_read_is_discarded_and_retried(market, monkeypatch):
    original = market.reader.prices
    calls = []

    def prices(*args):
        frame = original(*args)
        calls.append(True)
        if len(calls) == 1:
            path = market.root / f"kline_daily/date={market.days[80]}/part.parquet"
            pl.read_parquet(path).with_columns(
                (pl.col("amount") * 2).alias("amount")
            ).write_parquet(path)
        return frame

    monkeypatch.setattr(market.reader, "prices", prices)
    row = service.get_snapshot(market.repo, mode="event", event_date=market.days[80]).payload[
        "rows"
    ][0]
    assert len(calls) == 2
    assert row["latest"]["amount_ratio"] == pytest.approx(4)


def test_split_adjustment_amount_units_and_future_core_ranking(market):
    day = market.days[81]
    raw_path = market.root / f"kline_daily/date={day}/part.parquet"
    enriched_path = market.root / f"kline_daily_enriched/date={day}/part.parquet"
    old = service.get_snapshot(market.repo, mode="event", event_date=market.days[80]).payload[
        "rows"
    ][0]
    pl.read_parquet(raw_path).with_columns(
        *[(pl.col(c) / 2).alias(c) for c in ("open", "high", "low", "close")],
        pl.when(pl.col("symbol") == market.symbols[0])
        .then(1e12)
        .otherwise(pl.col("amount"))
        .alias("amount"),
    ).write_parquet(raw_path)
    pl.read_parquet(enriched_path).with_columns(
        (pl.col("raw_close") / 2).alias("raw_close")
    ).write_parquet(enriched_path)
    row = service.get_snapshot(
        market.repo, mode="event", event_date=market.days[80], view_as_of=day
    ).payload["rows"][0]
    assert row["observations"][1]["day_return"] == pytest.approx(0.001)
    assert row["observations"][1]["amount"] > 1e12
    assert [c["symbol"] for c in row["core_members"]] == [c["symbol"] for c in old["core_members"]]
    assert row["observations"][0] == old["observations"][0]


def test_zero_index_amount_and_duplicate_daily_rows(market):
    day = market.days[80]
    path = market.root / f"kline_index_daily/date={day}/part.parquet"
    pl.read_parquet(path).with_columns(pl.lit(0.0).alias("amount")).write_parquet(path)
    row = service.get_snapshot(market.repo, mode="event", event_date=day).payload["rows"][0]
    assert row["observations"][0]["amount_expansion"] is False
    raw_path = market.root / f"kline_daily/date={day}/part.parquet"
    frame = pl.read_parquet(raw_path)
    pl.concat([frame, frame.head(1)]).write_parquet(raw_path)
    result = market.client.get("/api/sector-review/event", params={"event_date": str(day)}).json()
    assert result["status"] == "unavailable"
    assert "重复" in result["warnings"][0]


def test_readonly_old_configs_do_not_migrate(tmp_path):
    from app.services.ext_data import ExtConfigStore

    old = tmp_path / "ext_configs.json"
    old.write_text('[{"id":"legacy","label":"旧分类","mode":"snapshot","fields":[]}]')
    before = old.read_bytes()
    assert ExtConfigStore(tmp_path).load_all_readonly() == []
    assert old.read_bytes() == before
    assert not (tmp_path / "ext_data").exists()


def test_classification_identity_duplicates_and_policy(market):
    path = market.root / "ext_data/test_industry/part.parquet"
    rows = pl.read_parquet(path)
    pl.concat([rows, rows]).write_parquet(path)
    first = service.get_snapshot(market.repo, mode="event", event_date=market.days[80]).payload
    assert first["rows"][0]["original_members"] == 6
    deeper = service.get_snapshot(
        market.repo, mode="event", event_date=market.days[80], level=2
    ).payload
    assert first["rows"][0]["sector_id"] != deeper["rows"][0]["sector_id"]
    rows.head(4).write_parquet(path)
    small = service.get_snapshot(market.repo, mode="event", event_date=market.days[80]).payload
    assert small["rows"][0]["candidate"] is True
    assert not small["rows"][0]["eligible_for_comparison"]
    assert small["response_overview"]["first_strength_counts"][str(market.days[80])] == 0


def test_rules_three_values():
    assert rules.tri_and(False, None) is False
    assert rules.tri_and(True, None) is None
    assert rules.tri_or(True, None) is True
    assert (
        rules.observation(
            date(2026, 1, 1),
            {"day_return": 0.0001, "amount_ratio": 1.01, "breadth_up": 0.51},
            {"day_return": 0.0001, "amount_ratio": 1.01},
        )["daily_resonance_candidate"]
        is True
    )


def test_shared_review_requires_login_but_is_available_to_guest(market):
    from app.services.auth import AuthStore
    from app.services.auth_access import install_auth_middleware
    from app.services.collector_proxy import collector_owns

    app = FastAPI()
    store = AuthStore(market.root / "test_auth")
    store.bootstrap("admin", "initial-secret")
    store.create_user("guest", "temporary-secret", "guest", None)
    token, _ = store.login("guest", "temporary-secret", "ip", "")
    token, _ = store.change_password(token, "temporary-secret", "permanent-secret")
    app.state.auth_store, app.state.repo = store, market.repo
    app.include_router(router)
    install_auth_middleware(app)
    client = TestClient(app)
    path = "/api/sector-review/event"
    assert client.get(path).status_code == 401
    client.cookies.set("tf_session", token)
    assert client.get(path, params={"event_date": str(market.days[80])}).status_code == 200
    assert not collector_owns("GET", path)
