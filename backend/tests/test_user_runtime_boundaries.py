import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import polars as pl
import pytest

from app.services.internal_auth import credential
from app.services.shared_runtime import QuoteMirror
from app.services.strategy_run_queue import StrategyRunManager
from app.services.user_context import UserContext, current_user, user_scope
from app.services.user_migration import migrate_legacy_private_data
from app.strategy import custom_signals

UID_A = 'a' * 32
UID_B = 'b' * 32


def test_background_users_skip_idle_engines_but_keep_offline_tasks(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from app.services import auth, preferences
    from app.services.user_runtime import UserRuntimeRegistry
    from app.strategy import monitor_rules, paper

    users = [{'id': UID_A, 'role': 'admin'}, {'id': UID_B, 'role': 'user'}]
    monkeypatch.setattr(auth, 'get_store', lambda root: SimpleNamespace(list_active_users=lambda: users))
    monkeypatch.setattr(monitor_rules, 'load_all', lambda root: [])
    monkeypatch.setattr(preferences, 'get_strategy_monitor_enabled', lambda: False)
    registry = UserRuntimeRegistry(SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))))
    get = Mock(side_effect=lambda user: SimpleNamespace(user=user))
    monkeypatch.setattr(registry, 'get', get)
    assert registry.background_active() == []
    get.assert_not_called()
    # Persisted admin rules must continue to run without a browser session.
    monkeypatch.setattr(monitor_rules, 'load_all', lambda root: [{'enabled': True}])
    assert [r.user.id for r in registry.background_active()] == [UID_A]
    with user_scope(UserContext(UID_B, 'user', tmp_path)):
        path = paper.accounts_base(tmp_path) / 'paper-account' / 'account.json'
        path.parent.mkdir(parents=True)
        path.write_text('{}')
    assert [r.user.id for r in registry.background_active()] == [UID_A, UID_B]


def _runtime_strategy_code(strategy_id):
    return f'''import polars as pl
META = {{"id": {strategy_id!r}, "name": "可信测试策略", "asset_types": ["stock"], "timeframes": ["1d"]}}
BASIC_FILTER = {{"enabled": False}}
def filter(df, params):
    return pl.lit(True)
'''


@pytest.fixture
def runtime_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import settings as settings_api
    from app.api import strategy as strategy_api
    from app.config import settings
    from app.services.auth import AuthStore
    from app.services.auth_access import install_auth_middleware
    from app.services.user_runtime import UserRuntimeRegistry

    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    store = AuthStore(tmp_path)
    admin = store.bootstrap('admin', 'initial-password')
    guest = store.create_user('guest', 'temporary-password')
    token, _ = store.login('guest', 'temporary-password', 'test', '')
    token, _ = store.change_password(token, 'temporary-password', 'changed-password')
    owners = [UserContext(row['id'], row['role'], tmp_path) for row in (guest, admin)]
    templates = tmp_path / 'strategies' / 'templates'
    templates.mkdir(parents=True)
    for sid in ('trusted_one', 'trusted_two'):
        (templates / f'{sid}.py').write_text(_runtime_strategy_code(sid), encoding='utf-8')
    app = FastAPI()
    app.state.auth_store = store
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.user_runtimes = UserRuntimeRegistry(app.state)
    app.include_router(settings_api.router)
    app.include_router(strategy_api.router)
    install_auth_middleware(app)
    with TestClient(app) as client:
        client.cookies.set('tf_session', token)
        yield client, app.state, owners
    app.state.user_runtimes.shutdown()


@pytest.fixture
def admin_runtime_client(runtime_client):
    client, state, owners = runtime_client
    token, _ = state.auth_store.login('admin', 'initial-password', 'test', '')
    client.cookies.set('tf_session', token)
    return client, state, list(reversed(owners))


@pytest.mark.parametrize('update,active', [
    ({'strategy_monitor_enabled': False}, set()),
    ({'strategy_monitor_ids': ['trusted_two']}, {'trusted_two'}),
])
def test_monitor_preferences_reload_only_the_current_users_rules(admin_runtime_client, tmp_path, update, active):
    from app.services import preferences
    from app.strategy import monitor_rules

    client, state, owners = admin_runtime_client
    monitors = []
    for owner in owners:
        with user_scope(owner):
            preferences.set_realtime_monitor_config({
                'strategy_monitor_enabled': True, 'strategy_monitor_ids': ['trusted_one'],
            })
        monitor = state.user_runtimes.get(owner).monitor_engine
        with user_scope(owner):
            monitor.evaluate(pl.DataFrame({'symbol': ['X'], 'close': [10.0]}))
        assert set(monitor.latest_strategy_results()) == ({'trusted_one'} if owner.role == 'admin' else set())
        monitors.append(monitor)
    response = client.put('/api/settings/preferences/realtime-monitor', json=update)
    assert response.status_code == 200, response.text
    with user_scope(owners[0]):
        rules = monitor_rules.load_all(tmp_path)
        assert {rule['strategy_id'] for rule in rules if rule['enabled']} == active
        if active:
            assert monitor_rules.load_one(tmp_path, 'mr_strategy_trusted_two')['name'] == '策略监控 · 可信测试策略'
    assert {rule['strategy_id'] for rule in monitors[0].rules.values()} == active
    assert monitors[0].latest_strategy_results() == {}
    with user_scope(owners[1]):
        assert monitor_rules.load_one(tmp_path, 'mr_strategy_trusted_one')['enabled'] is True
        assert preferences.get_strategy_monitor_enabled() is True
    assert monitors[1].rules == {}
    assert monitors[1].latest_strategy_results() == {}


def test_runtime_reconciles_previously_disabled_monitors(runtime_client, tmp_path):
    from app.services import preferences
    from app.strategy import monitor_rules

    _client, state, owners = runtime_client
    with user_scope(owners[0]):
        preferences.set_realtime_monitor_config({
            'strategy_monitor_enabled': False, 'strategy_monitor_ids': ['trusted_one'],
        })
        monitor_rules.migrate_strategy_monitors(tmp_path, ['trusted_one'], {'trusted_one': '旧规则'})
    monitor = state.user_runtimes.get(owners[0]).monitor_engine
    with user_scope(owners[0]):
        assert monitor_rules.load_one(tmp_path, 'mr_strategy_trusted_one')['enabled'] is False
    assert monitor.has_rule_type('strategy') is False


@pytest.mark.parametrize('initialized', [False, True])
def test_monitor_preferences_report_reload_failure_and_allow_retry(admin_runtime_client, tmp_path, monkeypatch, initialized):
    from app.services import preferences
    from app.strategy import monitor_rules

    client, state, owners = admin_runtime_client
    with user_scope(owners[0]):
        preferences.set_realtime_monitor_config({
            'strategy_monitor_enabled': True, 'strategy_monitor_ids': ['trusted_one'],
        })
        monitor_rules.migrate_strategy_monitors(tmp_path, ['trusted_one'], {'trusted_one': '旧规则'})
    monitor = state.user_runtimes.get(owners[0]).monitor_engine if initialized else None

    def fail_reload(_rules):
        raise RuntimeError('reload failed')

    def fail_initialization(*_args):
        raise OSError('migration failed')

    with monkeypatch.context() as patch:
        if initialized:
            patch.setattr(monitor, 'set_rules', fail_reload)
        else:
            patch.setattr(monitor_rules, 'migrate_strategy_monitors', fail_initialization)
        response = client.put('/api/settings/preferences/realtime-monitor', json={'strategy_monitor_enabled': False})
    assert response.status_code == 503
    with user_scope(owners[0]):
        assert preferences.get_strategy_monitor_enabled() is False
    if initialized:
        assert monitor.has_rule_type('strategy') is True
    retried = client.put('/api/settings/preferences/realtime-monitor', json={'strategy_monitor_enabled': False})
    assert retried.status_code == 200, retried.text
    assert state.user_runtimes.get(owners[0]).monitor_engine.has_rule_type('strategy') is False


@pytest.mark.parametrize('source', ['custom', 'ai', 'composite'])
def test_web_cannot_load_or_run_archived_personal_python(runtime_client, tmp_path, monkeypatch, source):
    from datetime import date

    from app.services.screener import ScreenerService
    from app.strategy.engine import StrategyDataContext

    client, _state, owners = runtime_client
    sid = f'archived_{source}'
    directory = tmp_path / 'users' / owners[0].id / 'strategies' / source
    directory.mkdir(parents=True)
    code = _runtime_strategy_code(sid) + '\nIMPORT_PROBE = pl.lit(731503)\n'
    archive = directory / f'{sid}.py'
    archive.write_text(code, encoding='utf-8')
    imported = []
    original_lit = pl.lit

    def observe_import(value, *args, **kwargs):
        if isinstance(value, int) and value == 731503:
            imported.append(sid)
        return original_lit(value, *args, **kwargs)

    monkeypatch.setattr(pl, 'lit', observe_import)
    monkeypatch.setattr(ScreenerService, 'build_strategy_context', lambda *args, **kwargs: StrategyDataContext(
        asset_type='stock', timeframe='1d', as_of=date(2026, 10, 13),
        current=pl.DataFrame({'symbol': ['X'], 'close': [10.0]}),
    ))
    response = client.post('/api/strategies/run', json={'strategy_id': sid, 'as_of': '2026-10-13'})
    assert response.status_code == 404, response.text
    assert imported == []
    listing = client.get('/api/strategies')
    assert listing.status_code == 200
    assert sid not in {item['id'] for item in listing.json()['strategies']}
    assert archive.read_text(encoding='utf-8') == code


@pytest.mark.parametrize('archive_kind', ['absent', 'python', 'syntax', 'encoding'])
def test_web_composite_still_saves_and_runs_trusted_children(runtime_client, tmp_path, monkeypatch, archive_kind):
    from datetime import date

    from app.services.screener import ScreenerService
    from app.strategy.engine import StrategyDataContext

    client, _state, owners = runtime_client
    imported = []
    archive = tmp_path / 'users' / owners[0].id / 'strategies' / 'composite' / 'archived_runtime.py'
    code = (_runtime_strategy_code('archived_runtime') + '\nIMPORT_PROBE = pl.lit(731503)\n').encode('utf-8')
    if archive_kind == 'syntax':
        code = b'def archived_runtime(:\n    pass\n'
    elif archive_kind == 'encoding':
        code = b'\xff\xfe damaged Python archive'
    if archive_kind != 'absent':
        archive.parent.mkdir(parents=True)
        archive.write_bytes(code)
    trusted_two = tmp_path / 'strategies' / 'templates' / 'trusted_two.py'
    trusted_two.write_text(
        _runtime_strategy_code('trusted_two').replace('pl.lit(True)', 'pl.lit(False)'), encoding='utf-8',
    )
    original_lit = pl.lit

    def observe_import(value, *args, **kwargs):
        if isinstance(value, int) and value == 731503:
            imported.append('archived_runtime')
        return original_lit(value, *args, **kwargs)

    monkeypatch.setattr(pl, 'lit', observe_import)
    monkeypatch.setattr(ScreenerService, 'build_strategy_context', lambda *args, **kwargs: StrategyDataContext(
        asset_type='stock', timeframe='1d', as_of=date(2026, 10, 13),
        current=pl.DataFrame({'symbol': ['X'], 'close': [10.0]}),
    ))
    saved = client.post('/api/strategies/composite/save', json={
        'strategy_id': 'composite_private', 'name': '个人叠加',
        'children': [{'strategy_id': 'trusted_one', 'weight': 1.0}],
    })
    assert saved.status_code == 200, saved.text
    reloaded = client.post('/api/strategies/reload')
    assert reloaded.status_code == 200, reloaded.text
    for sid in ('trusted_one', 'composite_private'):
        result = client.post('/api/strategies/run', json={'strategy_id': sid, 'as_of': '2026-10-13'})
        assert result.status_code == 200, result.text
        assert [row['symbol'] for row in result.json()['rows']] == ['X']
    updated = client.post('/api/strategies/composite/save', json={
        'strategy_id': 'composite_private', 'name': '更新叠加', 'mode': 'update',
        'children': [{'strategy_id': 'trusted_two', 'weight': 1.0}],
    })
    assert updated.status_code == 200, updated.text
    reloaded = client.post('/api/strategies/reload')
    assert reloaded.status_code == 200, reloaded.text
    assert client.get('/api/strategies/composite_private').json()['name'] == '更新叠加'
    result = client.post('/api/strategies/run', json={'strategy_id': 'composite_private', 'as_of': '2026-10-13'})
    assert result.status_code == 200, result.text
    assert result.json()['rows'] == []
    assert client.post('/api/strategies/run', json={
        'strategy_id': 'archived_runtime', 'as_of': '2026-10-13',
    }).status_code == 404
    assert imported == []
    if archive_kind != 'absent':
        assert archive.read_bytes() == code


@pytest.mark.parametrize('source', ['trusted_module', 'personal_declaration'])
def test_invalid_strategy_reload_retains_previous_registry(runtime_client, tmp_path, source):
    from app.api.strategy import _render_composite_code

    client, _state, owners = runtime_client
    assert client.get('/api/strategies/trusted_one').json()['name'] == '可信测试策略'
    templates = tmp_path / 'strategies' / 'templates'
    (templates / 'trusted_one.py').write_text(
        _runtime_strategy_code('trusted_one').replace('可信测试策略', '尚未成功加载的新名称'), encoding='utf-8',
    )
    if source == 'trusted_module':
        invalid = templates / 'invalid.py'
        code = 'META = {"id": "invalid"}\n'
    else:
        invalid = tmp_path / 'users' / owners[0].id / 'strategies' / 'composite' / 'composite_invalid.py'
        invalid.parent.mkdir(parents=True)
        code = _render_composite_code('composite_invalid', '', '', [], 'union', 0)
    invalid.write_text(code, encoding='utf-8')
    response = client.post('/api/strategies/reload')
    assert response.status_code == 400, response.text
    assert client.get('/api/strategies/trusted_one').json()['name'] == '可信测试策略'


def test_runtime_minute_strategy_keeps_personal_declarative_signals(runtime_client, tmp_path, monkeypatch):
    from datetime import date, datetime

    from app.services.screener import ScreenerService
    from app.strategy.engine import StrategyDataContext

    client, _state, owners = runtime_client
    trusted = tmp_path / 'strategies' / 'templates' / 'trusted_minute.py'
    trusted.write_text('''import polars as pl
META = {"id": "trusted_minute", "name": "分钟测试", "asset_types": ["stock"], "timeframes": ["1m"]}
EXECUTION_BACKEND = "minute_filter"
REQUIRED_FEATURES = ["csgi_personal"]
BASIC_FILTER = {"enabled": False}
def filter_minute_history(df, params):
    return df.filter(pl.col("csgi_personal"))
''', encoding='utf-8')
    with user_scope(owners[0]):
        custom_signals.save_one(tmp_path, {
            'id': 'personal', 'timeframe': 'intraday',
            'conditions': [{'left': 'price', 'op': '>', 'right': '10'}],
        })
    bars = pl.DataFrame({
        'symbol': ['X', 'X'],
        'datetime': [datetime(2026, 10, 13, 9, 30), datetime(2026, 10, 13, 9, 31)],
        'close': [9.0, 11.0],
        'volume': [100.0, 100.0], 'amount': [90000.0, 110000.0],
    })
    monkeypatch.setattr(ScreenerService, 'build_strategy_context', lambda *args, **kwargs: StrategyDataContext(
        asset_type='stock', timeframe='1m', as_of=date(2026, 10, 13), history=bars,
    ))
    result = client.post('/api/strategies/run', json={
        'strategy_id': 'trusted_minute', 'as_of': '2026-10-13', 'timeframe': '1m',
    })
    assert result.status_code == 200, result.text
    assert [(row['symbol'], row['close']) for row in result.json()['rows']] == [('X', 11.0)]


def test_migration_is_initial_owner_only_and_never_overwrites(tmp_path):
    legacy = tmp_path / 'user_data'
    legacy.mkdir()
    (legacy / 'watchlist_groups.json').write_text('[{"id":"legacy"}]')
    (legacy / 'preferences.json').write_text(json.dumps({'nav_order': ['private'], 'feishu_webhook_secret': 'secret'}))
    target = tmp_path / 'users' / UID_A / 'user_data'
    target.mkdir(parents=True)
    (target / 'watchlist_groups.json').write_text('[{"id":"existing"}]')
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: migrate_legacy_private_data(tmp_path, UID_A), range(2)))
    assert json.loads((target / 'watchlist_groups.json').read_text()) == [{'id': 'existing'}]
    assert json.loads((target / 'preferences.json').read_text()) == {'nav_order': ['private']}
    assert not (tmp_path / 'users' / UID_B).exists()
    assert (legacy / 'watchlist_groups.json').exists()


def test_migration_failed_publish_can_retry_without_partial_preferences(tmp_path, monkeypatch):
    import app.services.user_migration as migration
    legacy = tmp_path / 'user_data'
    legacy.mkdir()
    (legacy / 'preferences.json').write_text('{"nav_order":["legacy"]}')
    with monkeypatch.context() as patch:
        def fail_link(*args):
            raise OSError('disk failure')
        patch.setattr(migration.os, 'link', fail_link)
        with pytest.raises(OSError):
            migrate_legacy_private_data(tmp_path, UID_A)
    target = tmp_path / 'users' / UID_A / 'user_data' / 'preferences.json'
    assert not target.exists()
    migrate_legacy_private_data(tmp_path, UID_A)
    assert json.loads(target.read_text()) == {'nav_order': ['legacy']}


def test_internal_credential_concurrent_first_start(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(lambda _: credential(tmp_path), range(16)))
    assert len(set(keys)) == 1
    assert len(keys[0]) == 64
    assert credential(tmp_path) == keys[0]


def test_queue_same_job_key_different_owners_never_piggybacks(tmp_path):
    manager = StrategyRunManager()
    gate = threading.Event()
    seen = []
    def work(handle):
        gate.wait(2)
        seen.append(current_user().id)
        handle.complete('s', {'owner': current_user().id})
    with user_scope(UserContext(UID_A, 'admin', tmp_path)):
        a = manager.get_or_submit(('same',), ['s'], work)
    with user_scope(UserContext(UID_B, 'guest', tmp_path)):
        b = manager.get_or_submit(('same',), ['s'], work)
    assert a is not b
    gate.set()
    import time
    deadline = time.monotonic() + 3
    while not b.snapshot()['done'] and time.monotonic() < deadline:
        time.sleep(.01)
    assert seen == [UID_A, UID_B]
    assert a.snapshot()['results']['s']['owner'] == UID_A
    assert b.snapshot()['results']['s']['owner'] == UID_B


def test_sse_mirror_filters_private_alerts_and_reports(tmp_path):
    mirror = QuoteMirror(SimpleNamespace())
    with user_scope(UserContext(UID_A, 'admin', tmp_path)):
        a = mirror.subscribe()
    with user_scope(UserContext(UID_B, 'guest', tmp_path)):
        b = mirror.subscribe()
    mirror.dispatch('strategy_alert', {'owner_id': UID_A, 'alerts': [{'message': 'a'}]})
    mirror.dispatch('review_progress', {'owner_id': UID_B, 'text': 'b'})
    mirror.dispatch('strategy_alert', {'alerts': [{'message': 'unowned'}]})
    assert a.pop()['alerts'] == [{'message': 'a'}]
    snapshot = b.pop()
    assert snapshot['alerts'] == []
    assert json.loads(snapshot['reviews'][0])['text'] == 'b'


def test_personal_signals_do_not_modify_shared_frame_or_other_user(tmp_path):
    shared = pl.DataFrame({'symbol': ['X'], 'close': [5.]})
    for uid, value in ((UID_A, '1'), (UID_B, '10')):
        with user_scope(UserContext(uid, 'guest', tmp_path)):
            custom_signals.save_one(tmp_path, {'id': 'same', 'conditions': [{'left': 'close', 'op': '>', 'right': value}]})
    with user_scope(UserContext(UID_A, 'admin', tmp_path)):
        a = custom_signals.apply_user_signals(shared)
    with user_scope(UserContext(UID_B, 'guest', tmp_path)):
        b = custom_signals.apply_user_signals(shared)
    assert a['csg_same'][0] is True
    assert b['csg_same'][0] is False
    assert shared.columns == ['symbol', 'close']
    assert custom_signals.apply_user_signals(shared) is shared


def test_web_collector_forward_keeps_owner_and_rewrites_validated_origin(tmp_path):
    import asyncio

    import httpx
    from fastapi import FastAPI, Request

    from app.services.auth import AuthStore
    from app.services.auth_access import install_auth_middleware
    from app.services.collector_proxy import proxy_middleware
    store = AuthStore(tmp_path)
    user = store.bootstrap('admin', 'initial-password')
    token, _ = store.login('admin', 'initial-password', 'ip', 'browser')
    collector = FastAPI()
    collector.state.service_role = 'collector'
    collector.state.auth_store = store
    @collector.post('/api/watchlist/check')
    def check(request: Request):
        return {'owner': current_user().id, 'origin': request.headers.get('origin')}
    install_auth_middleware(collector)
    web = FastAPI()
    web.state.service_role = 'web'
    web.state.auth_store = store
    web.state.collector_url = 'http://collector:3019'
    web.middleware('http')(proxy_middleware)
    install_auth_middleware(web)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=collector)) as internal:
            web.state.collector_client = internal
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url='http://panel') as browser:
                browser.cookies.set('tf_session', token)
                response = await browser.post('/api/watchlist/check', headers={'Origin': 'http://panel'})
                assert response.status_code == 200, response.text
                assert response.json() == {'owner': user['id'], 'origin': 'http://collector:3019'}
                blocked = await browser.post('/api/watchlist/check', headers={'Origin': 'https://evil.example'})
                assert blocked.status_code == 403
                store.revoke_session(token)
                assert (await browser.post('/api/watchlist/check', headers={'Origin': 'http://panel'})).status_code == 401
    asyncio.run(run())


def test_migration_rejects_symlinked_parent_and_does_not_finish(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'watchlist_groups.json').write_text('[]')
    root = tmp_path / 'data'
    root.mkdir()
    (root / 'user_data').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='符号链接'):
        migrate_legacy_private_data(root, UID_A)
    (root / 'user_data').unlink()
    (root / 'user_data').mkdir()
    (root / 'user_data' / 'watchlist_groups.json').write_text('[{"id":"safe"}]')
    migrate_legacy_private_data(root, UID_A)
    assert json.loads((root / 'users' / UID_A / 'user_data' / 'watchlist_groups.json').read_text()) == [{'id': 'safe'}]


def test_monitor_round_restores_each_owner_and_never_uses_global_engine(tmp_path):
    from app.services.quote_service import QuoteService
    owners = [UserContext(UID_A, 'admin', tmp_path), UserContext(UID_B, 'guest', tmp_path)]
    runtimes = [SimpleNamespace(user=user, monitor_engine=object()) for user in owners]
    service = QuoteService()
    service._app_state = SimpleNamespace(user_runtimes=SimpleNamespace(background_active=lambda: runtimes))
    seen = []
    service._evaluate_user_monitors = lambda daily, quotes, runtime, **kwargs: seen.append((current_user().id, runtime.monitor_engine))
    service._evaluate_monitors(pl.DataFrame(), None)
    assert seen == [(runtime.user.id, runtime.monitor_engine) for runtime in runtimes]
    assert current_user(required=False) is None


def test_delayed_notification_rechecks_admin_role(tmp_path):
    from app.services.auth import AuthStore
    from app.services.external_notifications import submit
    store = AuthStore(tmp_path)
    admin = store.bootstrap('admin', 'long-initial-password')
    queued = []
    delivered = []
    executor = SimpleNamespace(submit=lambda fn: queued.append(fn))
    with user_scope(UserContext(admin['id'], 'admin', tmp_path)):
        submit(executor, delivered.append, 'private-alert')
    # Simulate a maintenance change after enqueueing; account management protects admin.
    with store._connection(True) as db:
        db.execute("UPDATE users SET role='guest' WHERE id=?", (admin['id'],))
    queued[0]()
    assert delivered == []


def test_signal_edits_do_not_clear_shared_repository(tmp_path):
    from unittest.mock import Mock

    from app.api.signals import _invalidate
    from app.custom.assistant.tools import _invalidate_custom_signals
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), clear_cache=Mock())
    with user_scope(UserContext(UID_A, 'admin', tmp_path)):
        _invalidate(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo))))
        _invalidate_custom_signals(tmp_path, repo)
    repo.clear_cache.assert_not_called()


def test_runtime_managers_are_owner_bound_and_recover_only_explicitly(tmp_path, monkeypatch):
    from app.config import settings
    from app.services.auth import AuthStore
    from app.services.user_runtime import UserRuntimeRegistry
    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    store = AuthStore(tmp_path)
    admin = store.bootstrap('admin', 'long-initial-password')
    guest = store.create_user('guest', 'long-guest-password', actor_id=admin['id'])
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), service_role='collector')
    registry = UserRuntimeRegistry(state)
    for row in (admin, guest):
        runtime = registry.get(UserContext(row['id'], row['role'], tmp_path))
        assert runtime.mining_manager._owner.id == row['id']
        runtime.mining_manager.store.create({}, {}, run_id='same')
        assert runtime.mining_manager.store.get('same')['status'] == 'queued'
    web_registry = UserRuntimeRegistry(SimpleNamespace(repo=state.repo, service_role='web'))
    for row in (admin, guest):
        runtime = web_registry.get(UserContext(row['id'], row['role'], tmp_path))
        assert runtime.mining_manager.store.get('same')['status'] == 'queued'
    for runtime in registry.active():
        runtime.strategy_engine._realtime_matrices['old'] = object()
        runtime.monitor_engine._latest_strategy_results['old'] = {'rows': []}
    registry.invalidate_shared_strategy_state()
    for runtime in registry.active():
        assert runtime.strategy_engine._realtime_matrices == {}
        assert runtime.monitor_engine.latest_strategy_results() == {}
    registry.recover_interrupted()
    for runtime in registry.active():
        assert runtime.mining_manager.store.get('same')['status'] == 'interrupted'
    registry.shutdown()
    web_registry.shutdown()
    assert current_user(required=False) is None


def test_shared_ext_change_invalidates_every_users_result_cache(tmp_path):
    from app.factors.ext_factors import invalidate_ext_caches
    from app.services import strategy_cache
    from app.services.auth import AuthStore
    auth = AuthStore(tmp_path)
    admin = auth.bootstrap('admin', 'long-initial-password')
    guest = auth.create_user('guest', 'long-guest-password', actor_id=admin['id'])
    owners = [UserContext(row['id'], row['role'], tmp_path) for row in (admin, guest)]
    for owner in owners:
        with user_scope(owner):
            strategy_cache.write_cache(tmp_path, '2026-10-13', {'s': {'total': 1, 'rows': []}})
    # Background shared-data operations deliberately have no personal identity.
    invalidate_ext_caches(tmp_path, keep_strategy_cache=True)
    for owner in owners:
        with user_scope(owner):
            assert strategy_cache.read_cache(tmp_path) is not None
    invalidate_ext_caches(tmp_path)
    for owner in owners:
        with user_scope(owner):
            assert strategy_cache.read_cache(tmp_path) is None
