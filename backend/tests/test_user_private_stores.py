import pytest

from app.services import alert_store, preferences, watchlist
from app.services.user_context import UserContext, user_scope
from app.strategy import config, custom_signals, lots, monitor_rules, paper


def test_private_stores_are_bidirectionally_isolated(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    for uid in ('a' * 32, 'b' * 32):
        with user_scope(UserContext(uid, 'admin' if uid == 'a' * 32 else 'guest', tmp_path)):
            assert watchlist.list_symbols() == []
            watchlist.add(uid)
            preferences.save({'nav_order': [uid]})
            config.save_override(tmp_path, 'same', {'params': {'owner': uid}})
            monitor_rules.save_one(tmp_path, {'id': 'same', 'name': uid})
            lots.save_one(tmp_path, {'id': 'lot_same', 'symbol': uid})
            custom_signals.save_one(tmp_path, {'id': 'same', 'name': uid})
            paper.create_account(tmp_path, 100000, name=uid)
            alert_store.append_many(tmp_path, [{'ts': __import__('time').time() * 1000, 'message': uid}])
    for uid in ('a' * 32, 'b' * 32):
        with user_scope(UserContext(uid, 'admin' if uid == 'a' * 32 else 'guest', tmp_path)):
            assert [r['symbol'] for r in watchlist.list_symbols()] == [uid]
            assert preferences.get_nav_order() == [uid]
            assert config.load_override(tmp_path, 'same')['params']['owner'] == uid
            assert monitor_rules.load_one(tmp_path, 'same')['name'] == uid
            assert lots.load_all(tmp_path)[0]['symbol'] == uid
            assert custom_signals.load_all(tmp_path)[0]['name'] == uid
            assert paper.get_account(tmp_path)['name'] == uid
            assert alert_store.list_recent(tmp_path)[0]['message'] == uid
    with pytest.raises(RuntimeError):
        watchlist.list_symbols()
