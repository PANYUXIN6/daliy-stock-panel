"""交易日历仅由采集端更新；未知覆盖不得按星期猜测。"""
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.market_time import CN_TZ
from app.services import trading_day
from app.services.quote_service import QuoteService


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(trading_day, "cn_now", lambda: datetime(2026, 10, 10, 8, tzinfo=CN_TZ))
    from app.data_providers import custom
    provider = SimpleNamespace(trading_days=Mock(return_value={date(2026, 9, 30), date(2026, 10, 9)}))
    monkeypatch.setattr(custom, "is_custom_provider", lambda name: True)
    monkeypatch.setattr(custom, "get_provider", lambda name: provider)
    return tmp_path, provider


def test_unknown_calendar_stays_unknown_without_network(calendar):
    root, provider = calendar
    for day in (9, 10):
        assert trading_day.is_trading_day(datetime(2026, 10, day, 10, tzinfo=CN_TZ), data_dir=root) is None
    provider.trading_days.assert_not_called()


def test_persisted_calendar_shared_and_bounded(calendar, monkeypatch):
    root, provider = calendar
    assert trading_day.sync_calendar(root)["updated"] is True
    provider.trading_days.side_effect = AssertionError("read must not fetch")
    for _ in range(2):
        assert trading_day.is_trading_day(datetime(2026, 10, 9, 10, tzinfo=CN_TZ), data_dir=root) is True
        assert trading_day.is_trading_day(datetime(2026, 10, 1, 10, tzinfo=CN_TZ), data_dir=root) is False
        assert trading_day.is_trading_day(datetime(2026, 10, 11, 10, tzinfo=CN_TZ), data_dir=root) is None
    assert provider.trading_days.call_count == 1


@pytest.mark.parametrize("verdict", [False, None])
def test_quote_stops_on_holiday_or_unknown_calendar(monkeypatch, verdict):
    monkeypatch.setattr(trading_day, "is_trading_day", lambda *args, **kwargs: verdict)
    service = QuoteService()
    for phase in ("morning", "afternoon", "close_final"):
        assert service._should_poll_for_phase(phase) is False


@pytest.mark.parametrize("hour,minute,phase", [(9, 29, "closed"), (9, 30, "morning"), (11, 30, "closed"), (12, 55, "closed"), (13, 0, "afternoon"), (15, 0, "close_final")])
def test_quote_sessions_and_final_confirmation(monkeypatch, hour, minute, phase):
    from app.services import quote_service
    now = datetime(2026, 10, 9, hour, minute, tzinfo=CN_TZ)
    monkeypatch.setattr(quote_service, "cn_now", lambda: now)
    monkeypatch.setattr(quote_service, "cn_today", lambda: now.date())
    monkeypatch.setattr(trading_day, "is_trading_day", lambda *args, **kwargs: True)
    service = QuoteService()
    assert service._market_phase() == phase
    if phase == "close_final":
        assert service._should_poll_for_phase(phase)
        service._final_sync_done.add((now.date(), "close"))
        assert not service._should_poll_for_phase(phase)


def test_fuyao_provider_trading_days_conversion(monkeypatch):
    from app.plugins.fuyao import provider as fp
    from app.plugins.fuyao.provider import FuyaoProvider

    class _CalClient:
        def trading_days(self):
            # 上海零点戳按 provider 的 _ms_of_date 口径构造
            def _midnight_ms(d):
                import calendar
                return (calendar.timegm(d.timetuple()) - 28800) * 1000

            d4, d7 = date(2026, 9, 4), date(2026, 9, 7)
            return [
                {"date": "20260904", "date_ms": _midnight_ms(d4)},
                {"date": "20260907", "date_ms": _midnight_ms(d7)},
            ]

    monkeypatch.setattr(
        fp, "fuyao_client", type("M", (), {"FuyaoClient": lambda **kw: _CalClient()})
    )
    monkeypatch.setattr(fp, "get_api_key", lambda: "test-key")
    days = FuyaoProvider().trading_days()
    assert days == {date(2026, 9, 4), date(2026, 9, 7)}


def test_calendar_disjoint_refresh_does_not_invent_unknown_coverage(calendar, monkeypatch):
    root, provider = calendar
    trading_day.sync_calendar(root)
    provider.trading_days.return_value = {date(2028, 9, 29), date(2028, 10, 9)}
    monkeypatch.setattr(trading_day, 'cn_now', lambda: datetime(2028, 10, 10, 8, tzinfo=CN_TZ))
    trading_day.sync_calendar(root)
    assert trading_day.is_trading_day(datetime(2027, 10, 9, 10, tzinfo=CN_TZ), data_dir=root) is None
    assert trading_day.is_trading_day(datetime(2028, 10, 9, 10, tzinfo=CN_TZ), data_dir=root) is True
