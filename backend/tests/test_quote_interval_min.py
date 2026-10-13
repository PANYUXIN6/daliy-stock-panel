"""统一行情采集的默认周期与最低周期均为 30 秒。"""
from __future__ import annotations

from app.services.quote_service import QuoteService


def _route(monkeypatch, provider: str, tier: str) -> None:
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: provider)
    monkeypatch.setattr(QuoteService, "_current_tier", classmethod(lambda cls: tier))


def _bare() -> QuoteService:
    return QuoteService.__new__(QuoteService)


def test_custom_provider_min_interval_30s(monkeypatch):
    _route(monkeypatch, "fuyao", "none")
    assert _bare().get_min_interval() == 30.0


def test_custom_provider_clamp_enforces_30s(monkeypatch):
    _route(monkeypatch, "fuyao", "none")
    qs = _bare()
    assert qs._clamp_interval(1.0) == 30.0
    assert qs._clamp_interval(0.5) == 30.0
    assert qs._clamp_interval(120.0) == QuoteService.MAX_INTERVAL


def test_tickflow_tier_floor_uniform(monkeypatch):
    for tier, expect in ((tier, 30.0) for tier in ("none", "free", "starter", "pro", "expert")):
        _route(monkeypatch, "tickflow", tier)
        assert _bare().get_min_interval() == expect, tier


def test_default_interval_unchanged():
    assert QuoteService.DEFAULT_INTERVAL == 30.0
