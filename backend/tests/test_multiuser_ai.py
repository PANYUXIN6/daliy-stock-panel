import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.auth import AuthStore
from app.services.user_context import UserContext, user_data_dir, user_scope


@pytest.fixture
def actors(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    store = AuthStore(tmp_path)
    admin = store.bootstrap('admin', 'initial-secret')
    token, _ = store.login('admin', 'initial-secret', 'ip1', '')
    first = UserContext(admin['id'], 'admin', tmp_path, session_token=token)
    guest = store.create_user('guest', 'temporary-secret')
    token, _ = store.login('guest', 'temporary-secret', 'ip2', '')
    token, _ = store.change_password(token, 'temporary-secret', 'changed-secret')
    second = UserContext(guest['id'], 'guest', tmp_path, session_token=token)
    return store, first, second


async def test_reports_isolate_threads_and_all_roles(actors):
    from app.services import ai_reports, market_recap_reports, stock_reports
    _store, first, second = actors
    for module in (ai_reports, stock_reports, market_recap_reports):
        with user_scope(first):
            report = await asyncio.to_thread(module.save_report, {'content': 'admin private'})
        with user_scope(second):
            assert module.list_reports() == []
            assert not module.delete_report(report['id'])
            other = module.save_report({'content': 'guest private'})
        with user_scope(first):
            assert [r['content'] for r in module.list_reports()] == ['admin private']
            assert not module.delete_report(other['id'])
    assert (user_data_dir(first.data_dir, first.id) / 'user_data' / 'ai_reports.json').exists()


async def test_pending_actions_cross_user_and_replaced_session(actors):
    from app.custom.assistant.actions import PendingRegistry
    store, first, second = actors
    registry = PendingRegistry(timeout_s=0.02)
    with user_scope(first):
        action = await registry.register('known-id', 'add_to_watchlist', {'symbol': '000001.SZ'})
    with user_scope(second):
        assert await registry.resolve('known-id', True) is None
    with user_scope(first):
        assert await registry.resolve('known-id', True) is action
        store.revoke_session(first.session_token)
        assert await registry.await_decision(action) == 'expired'


async def test_revoked_assistant_cannot_write(actors):
    from app.custom.assistant.tools import ToolContext, execute_assistant_tool
    store, _first, second = actors
    store.revoke_session(second.session_token)
    with user_scope(second):
        result = await execute_assistant_tool('add_to_watchlist', {'symbol': '000001.SZ'}, ToolContext(data_dir=second.data_dir))
        assert not result['ok']
        from app.services import watchlist
        assert watchlist.list_symbols() == []


async def test_legacy_cli_configuration_cannot_launch_process(monkeypatch):
    from app.services import ai_provider
    monkeypatch.setattr(ai_provider, 'current_ai_provider', lambda: 'codex_cli')
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: pytest.fail('CLI launched'))
    assert not ai_provider.ai_configured()
    assert not ai_provider.codex_cli_available()
    with pytest.raises(RuntimeError, match='停用'):
        await ai_provider.generate_ai_text([{'role': 'user', 'content': 'test'}])
    with pytest.raises(RuntimeError, match='停用'):
        _ = [part async for part in ai_provider.stream_ai_text([{'role': 'user', 'content': 'test'}])]


async def test_scheduled_review_is_owned_and_disabled_owner_cannot_run(actors, monkeypatch):
    from app.jobs import daily_pipeline
    from app.services import market_recap_reports, preferences, shared_runtime
    from app.services.user_context import current_user
    store, first, second = actors
    monkeypatch.setattr('app.services.trading_day.is_trading_day', lambda: True)
    monkeypatch.setattr('app.secrets_store.get_ai_key', lambda: 'test-only')
    captured = []
    async def generate(*args):
        captured.append(current_user().id)
        return 'private review', {'as_of': '2026-10-13'}
    monkeypatch.setattr(daily_pipeline, '_stream_review_with_retry', generate)
    monkeypatch.setattr(daily_pipeline, '_get_app_state', lambda: None)
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda *args, **kwargs: None)
    with user_scope(first):
        preferences.set_review_schedule(True, 15, 40)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=first.data_dir))
    await daily_pipeline._run_scheduled_review(repo)
    assert captured == [first.id]
    with user_scope(first):
        assert market_recap_reports.list_reports()[0]['content'] == 'private review'
    with user_scope(second):
        assert market_recap_reports.list_reports() == []
    store.create_user('otheradmin', 'temporary-secret', 'admin')
    store.update_user(first.id, {'is_active': False})
    await daily_pipeline._run_scheduled_review(repo)
    assert captured == [first.id]


def test_report_http_crud_is_private_even_for_admin(actors):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import financials, market_recap, stock_analysis
    from app.services.auth_access import install_auth_middleware
    store, first, second = actors
    app = FastAPI()
    app.state.auth_store = store
    for router in (financials.router, stock_analysis.router, market_recap.router):
        app.include_router(router)
    install_auth_middleware(app)
    with TestClient(app) as admin, TestClient(app) as guest:
        admin.cookies.set('tf_session', first.session_token)
        guest.cookies.set('tf_session', second.session_token)
        for base in ('/api/financials', '/api/stock-analysis', '/api/market-recap'):
            created = guest.post(base + '/reports', json={'symbol': '000001.SZ', 'as_of': '2026-10-13', 'content': 'guest-private'}).json()['report']
            assert admin.get(base + '/reports').json() == {'reports': []}
            assert admin.delete(base + '/reports/' + created['id']).json() == {'ok': False}
            own = admin.post(base + '/reports', json={'symbol': '000001.SZ', 'as_of': '2026-10-13', 'content': 'admin-private'}).json()['report']
            assert guest.delete(base + '/reports/' + own['id']).json() == {'ok': False}
            assert guest.get(base + '/reports').json()['reports'][0]['content'] == 'guest-private'


async def test_stream_revocation_prevents_archive_and_private_publication(actors, monkeypatch):
    import json

    from app.services import shared_runtime, stock_reports
    from app.services.analysis_archive import archive_analysis
    store, first, second = actors
    published = []
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda *args, **kwargs: published.append((args, kwargs)))
    async def revoked_stream():
        yield json.dumps({'type': 'delta', 'content': 'partial'})
        store.revoke_session(second.session_token)
        yield json.dumps({'type': 'done'})
    with user_scope(second):
        events = [json.loads(part) async for part in archive_analysis(revoked_stream(), stock_reports.save_report, second.data_dir, 'analysis', {})]
        assert stock_reports.list_reports() == []
    assert not published
    assert any(event.get('code') == 'SESSION_INVALID' for event in events)
    async def complete_stream():
        yield json.dumps({'type': 'delta', 'content': 'complete'})
        yield json.dumps({'type': 'done'})
    with user_scope(first):
        events = [json.loads(part) async for part in archive_analysis(complete_stream(), stock_reports.save_report, first.data_dir, 'analysis', {})]
        assert stock_reports.list_reports()[0]['content'] == 'complete'
    assert events[-1]['archived']
    assert published[0][1] == {'private': True}


def test_review_external_delivery_requires_current_active_admin(actors, monkeypatch):
    from app.jobs.daily_pipeline import _maybe_push_review
    from app.services import preferences, webhook_adapter
    store, first, second = actors
    sent = []
    monkeypatch.setattr(preferences, 'get_review_push_channels', lambda: ['feishu'])
    monkeypatch.setattr(preferences, 'get_feishu_webhook_url', lambda: 'https://example.invalid/test-only')
    monkeypatch.setattr(preferences, 'get_feishu_webhook_secret', lambda: '')
    monkeypatch.setattr(webhook_adapter, 'send_feishu_card', lambda *args: sent.append(args) or True)
    with user_scope(second):
        _maybe_push_review('private guest report', {})
    assert not sent
    with user_scope(first):
        _maybe_push_review('private admin report', {})
    assert len(sent) == 1 and sent[0][3] == 'private admin report'
    store.revoke_session(first.session_token)
    with user_scope(first):
        _maybe_push_review('stale admin report', {})
    assert len(sent) == 1


def test_ai_python_draft_cannot_be_saved_or_hot_loaded_by_any_role(actors):
    from app.strategy.ai_iterator import AIStrategyIterator
    _store, first, second = actors
    for actor in (first, second):
        with user_scope(actor), pytest.raises(PermissionError, match='Python'):
            AIStrategyIterator._save_draft(SimpleNamespace(reload=lambda: pytest.fail('hot loaded')), str(actor.data_dir), 'ai_test', 'raise RuntimeError()', {})
    assert not (first.data_dir / 'strategies' / 'ai' / 'ai_test.py').exists()
