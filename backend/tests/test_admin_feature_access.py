import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.services.auth import AuthStore


@pytest.mark.parametrize('service_role', ['web', 'collector'])
@pytest.mark.parametrize('role', [None, 'guest', 'pro', 'admin'])
def test_feature_routes_require_admin(tmp_path, monkeypatch, service_role, role):
    from app.config import settings
    from app.main import create_app

    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    from app.services import collector_proxy

    async def no_proxy(request, call_next):
        return await call_next(request)

    monkeypatch.setattr(collector_proxy, 'proxy_middleware', no_proxy)
    store = AuthStore(tmp_path)
    store.bootstrap('admin', 'initial-secret')
    token = None
    if role:
        if role != 'admin':
            store.create_user(role, 'initial-secret', role,
                              '2099-12-31' if role == 'pro' else None)
        token, _ = store.login(role, 'initial-secret', 'test', '')
        if role != 'admin':
            token, _ = store.change_password(token, 'initial-secret', 'changed-secret')

    @asynccontextmanager
    async def lifespan(app):
        app.state.auth_store = store
        yield

    app = create_app(service_role)
    app.router.lifespan_context = lifespan
    paths = {
        '/api/backtest/status', '/api/backtest/strategy/run',
        '/api/backtest/optimize/stream', '/api/backtest/walkforward/stream',
        '/api/monitor-rules', '/api/monitor-rules/rule-id',
        '/api/custom-signals', '/api/custom-signals/ai/generate',
        '/api/alerts', '/api/lots', '/api/lots/lot-id',
    }
    # 观察权限边界是否放行，避免执行真实回测、转发或写入用户数据。
    async def reached(scope, receive, send):
        from starlette.responses import JSONResponse
        await JSONResponse({'reached': True})(scope, receive, send)

    app.router.default = reached
    app.router.routes.clear()
    with TestClient(app) as client:
        if token:
            client.cookies.set('tf_session', token)
        for path in paths:
            for method in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'):
                response = client.request(method, path + '/')
                expected = 401 if role is None else 200 if role == 'admin' else 403
                assert response.status_code == expected, (method, path, response.text)
                if role == 'admin' and method != 'HEAD':
                    assert response.json() == {'reached': True}
                elif role not in {None, 'admin'} and method != 'HEAD':
                    assert response.json()['code'] == 'ADMIN_REQUIRED'


@pytest.mark.parametrize('service_role', ['web', 'collector'])
@pytest.mark.parametrize('role', [None, 'guest', 'pro', 'admin'])
def test_rotation_analysis_is_available_to_authenticated_users(
    tmp_path, monkeypatch, service_role, role,
):
    from app.api import rps
    from app.config import settings
    from app.main import create_app
    from app.services import collector_proxy

    monkeypatch.setattr(settings, 'data_dir', tmp_path)

    async def no_proxy(request, call_next):
        return await call_next(request)

    monkeypatch.setattr(collector_proxy, 'proxy_middleware', no_proxy)
    store = AuthStore(tmp_path)
    store.bootstrap('admin', 'initial-secret')
    token = None
    if role:
        if role != 'admin':
            store.create_user(role, 'initial-secret', role,
                              '2099-12-31' if role == 'pro' else None)
        token, _ = store.login(role, 'initial-secret', 'test', '')
        if role != 'admin':
            token, _ = store.change_password(token, 'initial-secret', 'changed-secret')

    generated = []

    async def analysis_stream(repo, days, focus, quote_service, depth_service, kind, level):
        generated.append(kind)
        yield json.dumps({'type': 'meta', 'days': days, 'summary': kind})
        yield json.dumps({'type': 'delta', 'content': f'{kind}:{level}:{focus}'})
        yield json.dumps({'type': 'done'})

    monkeypatch.setattr(rps, 'analyze_rotation_stream', analysis_stream)

    @asynccontextmanager
    async def lifespan(app):
        app.state.auth_store = store
        app.state.repo = SimpleNamespace()
        yield

    app = create_app(service_role)
    app.router.lifespan_context = lifespan
    with TestClient(app) as client:
        if token:
            client.cookies.set('tf_session', token)
        for kind, level in (('concept', None), ('industry', 2)):
            response = client.post('/api/rps/rotation-analyze', json={
                'days': 12, 'focus': '关注主线', 'kind': kind, 'level': level,
            })
            if role is None:
                assert response.status_code == 401
                assert response.json()['code'] == 'SESSION_INVALID'
                assert generated == []
                continue
            assert response.status_code == 200, response.text
            assert response.headers['content-type'].startswith('application/x-ndjson')
            assert [json.loads(line) for line in response.text.splitlines()] == [
                {'type': 'meta', 'days': 12, 'summary': kind},
                {'type': 'delta', 'content': f'{kind}:{level}:关注主线'},
                {'type': 'done'},
            ]
        if role in {'guest', 'pro'}:
            for method, path in (
                ('POST', '/api/rps/rotation'),
                ('POST', '/api/rps/rotation-analyze/private'),
                ('PUT', '/api/rps/rotation-analyze'),
                ('DELETE', '/api/rps/rotation-analyze'),
            ):
                denied = client.request(method, path, json={})
                assert denied.status_code == 403, (method, path, denied.text)
                assert denied.json()['code'] == 'ADMIN_REQUIRED'


@pytest.mark.parametrize('role', ['guest', 'pro'])
@pytest.mark.parametrize('tool', ['run_backtest', 'list_signals', 'create_signal_strategy', 'get_lots'])
async def test_assistant_cannot_bypass_feature_permissions(tmp_path, monkeypatch, tool, role):
    from app.config import settings
    from app.custom.assistant.tools import ToolContext, execute_assistant_tool
    from app.services.user_context import UserContext, user_scope

    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    store = AuthStore(tmp_path)
    store.bootstrap('admin', 'initial-secret')
    user = store.create_user(role, 'initial-secret', role,
                             '2099-12-31' if role == 'pro' else None)
    token, _ = store.login(role, 'initial-secret', 'test', '')
    token, _ = store.change_password(token, 'initial-secret', 'changed-secret')
    with user_scope(UserContext(user['id'], role, tmp_path, session_token=token)):
        result = await execute_assistant_tool(tool, {}, ToolContext(data_dir=tmp_path))
        if tool == 'run_backtest':
            from app.services.auth import AuthError
            from app.services.tool_catalog import run_backtest

            with pytest.raises(AuthError, match='此操作仅限管理员'):
                run_backtest(tmp_path, strategy_id='demo')
    assert result == {'ok': False, 'error': '此操作仅限管理员'}


@pytest.mark.parametrize('path', [
    '/api/backtest/factor/columns', '/api/backtest/factor/run',
    '/api/backtest/mining/runs', '/api/backtest/candidates', '/api/paper',
])
def test_other_features_keep_their_permissions(path):
    from app.services.auth_access import admin_required

    for method in ('GET', 'POST'):
        assert not admin_required(path, method)


@pytest.mark.parametrize('service_role', ['web', 'collector'])
@pytest.mark.parametrize('role', [None, 'guest', 'pro', 'admin'])
def test_strategy_overlay_metadata_remains_readable_without_opening_signals(
    tmp_path, monkeypatch, service_role, role,
):
    from app.config import settings
    from app.main import create_app
    from app.services import collector_proxy
    from app.services.user_runtime import UserRuntimeRegistry

    monkeypatch.setattr(settings, 'data_dir', tmp_path)

    async def no_proxy(request, call_next):
        return await call_next(request)

    monkeypatch.setattr(collector_proxy, 'proxy_middleware', no_proxy)
    store = AuthStore(tmp_path)
    store.bootstrap('admin', 'initial-secret')
    token = None
    if role:
        if role != 'admin':
            store.create_user(role, 'initial-secret', role,
                              '2099-12-31' if role == 'pro' else None)
        token, _ = store.login(role, 'initial-secret', 'test', '')
        if role != 'admin':
            token, _ = store.change_password(token, 'initial-secret', 'changed-secret')

    @asynccontextmanager
    async def lifespan(app):
        app.state.auth_store = store
        app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
        app.state.user_runtimes = UserRuntimeRegistry(app.state)
        try:
            yield
        finally:
            app.state.user_runtimes.shutdown()

    app = create_app(service_role)
    app.router.lifespan_context = lifespan
    with TestClient(app) as client:
        if token:
            client.cookies.set('tf_session', token)
        response = client.get('/api/custom-signals/options')
        if role is None:
            assert response.status_code == 401
            return
        assert response.status_code == 200
        options = response.json()
        assert {'close', 'volume'} <= {field['key'] for field in options['fields']}
        assert '>' in options['operators']
        assert 'signals' not in options
        strategy_id = 'active_limit_gene'
        for conditions in ([{'left': 'close', 'op': '>', 'right': '10'}], []):
            saved = client.post('/api/strategies/config', json={
                'strategy_id': strategy_id,
                'overrides': {'overlay_filter': conditions},
            })
            assert saved.status_code == 200, saved.text
            detail = client.get(f'/api/strategies/{strategy_id}')
            assert detail.status_code == 200, detail.text
            assert detail.json()['overlay_filter'] == conditions
        if role != 'admin':
            # 共享条件元数据不意味着开放信号定义、AI 或同前缀的其它路径。
            for method, path in (
                ('GET', '/api/custom-signals'),
                ('POST', '/api/custom-signals'),
                ('POST', '/api/custom-signals/options'),
                ('GET', '/api/custom-signals/options/private'),
                ('POST', '/api/custom-signals/ai/generate'),
            ):
                denied = client.request(method, path, json={})
                assert denied.status_code == 403, (method, path, denied.text)
                assert denied.json()['code'] == 'ADMIN_REQUIRED'
