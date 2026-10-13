"""Mixed close snapshots must not replace a newer close for another symbol."""
from datetime import datetime
from unittest.mock import Mock

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import quote_service
from app.services.quote_service import QuoteService
from app.tickflow.repository import DataStore, KlineRepository


@pytest.mark.parametrize("fresh_asset", ["stock", "etf", "index"])
@pytest.mark.parametrize("stale_offset", [-60_000, 10_000, None], ids=["pre_close", "older_final", "missing_ts"])
def test_final_snapshot_preserves_unconfirmed_symbols_and_partitions(
    tmp_path, monkeypatch, fresh_asset, stale_offset,
):
    now = datetime(2026, 10, 9, 23, tzinfo=CN_TZ)
    boundary = int(now.replace(hour=15).timestamp() * 1000)
    monkeypatch.setattr(quote_service, "cn_now", lambda: now)
    monkeypatch.setattr(quote_service, "cn_today", lambda: now.date())
    monkeypatch.setattr(quote_service, "_persist_last_fetch", lambda _ms: None)
    repo = KlineRepository(DataStore(tmp_path))
    symbols = {"stock": ["600000.SH", "600001.SH"],
               "etf": ["510300.SH", "510500.SH"],
               "index": ["000001.SH", "399001.SZ"]}
    monkeypatch.setattr(repo, "get_index_symbol_set", lambda: set(symbols["index"]))
    monkeypatch.setattr(repo, "get_etf_instruments", lambda: pl.DataFrame({"symbol": symbols["etf"]}))

    def record(symbol, price, timestamp):
        return {"symbol": symbol, "last_price": price, "open": price, "high": price,
                "low": price, "volume": 100.0, "amount": price * 100.0, "timestamp": timestamp}

    quote = QuoteService()
    quote.set_repo(repo)
    monkeypatch.setattr(quote, "_broadcast_quote_updated", Mock())
    # Keep the actual repository writes/cache semantics; indicator math is unrelated to freshness.
    def persist_enriched(df, _extra=None, asset_type="stock", merge=False):
        if merge:
            repo.merge_live_enriched_asset(asset_type, df)
        else:
            repo.flush_live_enriched_asset(asset_type, df)
    monkeypatch.setattr(quote, "_flush_live_enriched", persist_enriched)
    prior_records = []
    try:
        for asset, codes in symbols.items():
            rows = [record(code, 12.0, boundary + 30_000) for code in codes]
            prior_records.extend(rows)
            prior = quote._build_daily(rows)
            repo.flush_live_daily_asset(asset, prior)
            repo.flush_live_enriched_asset(asset, prior)
        quote._index_quotes_cache = quote._build_index_quotes(
            [r for r in prior_records if r["symbol"] in symbols["index"]],
        )
        incoming = [
            record(codes[0], 13.0 if asset == fresh_asset else 8.0,
                   boundary + 40_000 if asset == fresh_asset else
                   boundary + stale_offset if stale_offset is not None else None)
            for asset, codes in symbols.items()
        ]
        quote._process_full_market_records(
            incoming, t0=0, now_ts=1, final_boundary_ms=boundary,
        )
        assert quote._last_final_confirmed is False
        for asset, codes in symbols.items():
            table = {"stock": "kline_daily", "etf": "kline_etf_daily", "index": "kline_index_daily"}[asset]
            enriched_table = {"stock": "kline_daily_enriched", "etf": "kline_etf_enriched", "index": "kline_index_enriched"}[asset]
            for saved_table in [table, enriched_table]:
                saved = pl.read_parquet(tmp_path / saved_table / f"date={now.date()}" / "part.parquet")
                assert set(saved["symbol"]) == set(codes)
                changed = saved.filter(pl.col("symbol") == codes[0]).row(0, named=True)
                assert changed["close"] == (13.0 if asset == fresh_asset else 12.0)
                assert changed["quote_ts"] == boundary + (40_000 if asset == fresh_asset else 30_000)
                assert saved.filter(pl.col("symbol") == codes[1])["close"][0] == 12.0
        displayed = quote._index_quotes_cache.filter(pl.col("symbol") == symbols["index"][0])
        assert displayed["close"][0] == (13.0 if fresh_asset == "index" else 12.0)
    finally:
        repo.db.close()
