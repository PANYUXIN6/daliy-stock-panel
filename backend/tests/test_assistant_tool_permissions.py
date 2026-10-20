"""工具策略使用真实会话与 HTTP 边界，不调用外部模型。"""
import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.custom.assistant.permissions import (
    allowed_tool_names,
    get_tool_permissions,
    require_tool_access,
    set_tool_permissions,
)
from app.services.auth import AuthError, AuthStore
from app.services.user_context import UserContext, user_scope

GUEST_TOOLS = frozenset({
    'get_stock_quote', 'get_stock_daily', 'get_market_overview', 'get_indices',
    'get_sector_rotation', 'get_regime', 'get_abnormal', 'get_watchlist', 'add_to_watchlist',
})
PRO_TOOLS = GUEST_TOOLS | {
    'get_stock_analysis', 'get_financials', 'list_strategies', 'run_strategy',
    'list_factors', 'get_factor_values',
}
LOCKED_TOOLS = {'run_backtest', 'list_signals', 'create_signal_strategy', 'get_lots'}


@pytest.fixture
def actors(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    store = AuthStore(tmp_path)
    store.bootstrap('admin', 'initial-secret')
    owners = {}
    for role in ('admin', 'guest', 'pro'):
        if role != 'admin':
            store.create_user(role, 'initial-secret', role,
                              '2099-12-31' if role == 'pro' else None)
        token, _ = store.login(role, 'initial-secret', 'test', '')
        if role != 'admin':
            token, _ = store.change_password(token, 'initial-secret', 'changed-secret')
        user = store.session(token)['user']
        owners[role] = UserContext(user['id'], role, tmp_path, session_token=token)
    return store, owners


def defaults(owners):
    with user_scope(owners['admin']):
        return {tool['name']: tool['default_min_role'] for tool in get_tool_permissions()['tools']}


def config_path():
    return settings.data_dir / 'user_data' / 'assistant_tool_permissions.json'


def test_default_policy_metadata_matches_tools_and_expected_role_sets(actors):
    from app.custom.assistant import actions, tools
    from app.services import tool_catalog

    _store, owners = actors
    with user_scope(owners['admin']):
        response = get_tool_permissions()
        admin_tools = allowed_tool_names()
    raw_schemas = [*tool_catalog.build_tool_schemas(), *tools._local_tool_schemas()]
    assert admin_tools == {schema['function']['name'] for schema in raw_schemas}
    assert response['counts'] == {'guest': 9, 'pro': 15, 'admin': 24}
    assert response['configuration_error'] is None
    assert {tool['name'] for tool in response['tools'] if tool['locked']} == LOCKED_TOOLS
    assert {tool['name'] for tool in response['tools'] if tool['requires_confirmation']} == actions.ACTION_TOOLS
    assert all(tool['label'] and tool['description'] and tool['group'] for tool in response['tools'])
    for role, expected in (('guest', GUEST_TOOLS), ('pro', PRO_TOOLS)):
        with user_scope(owners[role]):
            assert allowed_tool_names() == expected
            for name in admin_tools:
                if name in expected:
                    require_tool_access(name)
                else:
                    with pytest.raises(AuthError) as denied:
                        require_tool_access(name)
                    assert denied.value.status == 403
                    if name in LOCKED_TOOLS:
                        assert denied.value.detail == '此操作仅限管理员'
    assert not config_path().exists()


def test_permission_changes_persist_and_recheck_without_stale_access(actors):
    _store, owners = actors
    permissions = defaults(owners)
    permissions['run_strategy'] = 'guest'
    permissions['get_stock_daily'] = 'admin'
    with user_scope(owners['admin']):
        saved = set_tool_permissions(permissions)
    assert saved['configuration_error'] is None
    assert json.loads(config_path().read_text()) == {'permissions': permissions}
    for role in ('guest', 'pro'):
        with user_scope(owners[role]):
            assert 'run_strategy' in allowed_tool_names()
            require_tool_access('run_strategy')
            with pytest.raises(AuthError, match='此操作仅限管理员'):
                require_tool_access('get_stock_daily')
    permissions['run_strategy'] = 'admin'
    with user_scope(owners['admin']):
        set_tool_permissions(permissions)
        assert len(allowed_tool_names()) == 24
    with user_scope(owners['guest']):
        assert 'run_strategy' not in allowed_tool_names()
        with pytest.raises(AuthError, match='此操作仅限管理员'):
            require_tool_access('run_strategy')


def test_current_session_role_and_revocation_override_captured_context(actors):
    store, owners = actors
    owner = owners['pro']
    with user_scope(owner):
        require_tool_access('get_financials')
        store.update_user(owner.id, {'role': 'guest'})
        assert allowed_tool_names() == GUEST_TOOLS
        with pytest.raises(AuthError, match='Pro'):
            require_tool_access('get_financials')
        store.revoke_session(owner.session_token)
        with pytest.raises(AuthError, match='会话已过期'):
            allowed_tool_names()
        with pytest.raises(AuthError, match='会话已过期'):
            require_tool_access('get_stock_quote')


@pytest.mark.parametrize('stored', ['not-json', '{}', '{"permissions": []}'])
def test_corrupt_configuration_fails_closed_and_admin_can_repair(actors, stored):
    _store, owners = actors
    permissions = defaults(owners)
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(stored)
    for role in ('guest', 'pro'):
        with user_scope(owners[role]):
            assert allowed_tool_names() == frozenset()
            with pytest.raises(AuthError, match='此操作仅限管理员'):
                require_tool_access('get_stock_quote')
    with user_scope(owners['admin']):
        assert len(allowed_tool_names()) == 24
        response = get_tool_permissions()
        assert response['configuration_error']
        assert response['counts'] == {'guest': 0, 'pro': 0, 'admin': 24}
        assert set_tool_permissions(permissions)['configuration_error'] is None
    with user_scope(owners['guest']):
        assert allowed_tool_names() == GUEST_TOOLS


def test_stored_locked_permissions_cannot_be_relaxed(actors):
    _store, owners = actors
    permissions = defaults(owners)
    for name in LOCKED_TOOLS:
        permissions[name] = 'guest'
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps({'permissions': permissions}))
    with user_scope(owners['admin']):
        response = get_tool_permissions()
        assert all(tool['min_role'] == 'admin' for tool in response['tools'] if tool['locked'])
        assert response['counts'] == {'guest': 9, 'pro': 15, 'admin': 24}
    with user_scope(owners['guest']):
        for name in LOCKED_TOOLS:
            with pytest.raises(AuthError, match='此操作仅限管理员'):
                require_tool_access(name)


@pytest.mark.parametrize('invalid', ['missing', 'unknown', 'role', 'locked'])
def test_invalid_changes_do_not_replace_saved_configuration(actors, invalid):
    _store, owners = actors
    permissions = defaults(owners)
    with user_scope(owners['admin']):
        set_tool_permissions(permissions)
        original = config_path().read_text()
        if invalid == 'missing':
            permissions.pop('get_stock_quote')
        elif invalid == 'unknown':
            permissions['unknown'] = 'guest'
        elif invalid == 'role':
            permissions['get_stock_quote'] = 'owner'
        else:
            permissions['run_backtest'] = 'pro'
        with pytest.raises(AuthError) as rejected:
            set_tool_permissions(permissions)
        assert rejected.value.status == 422
        assert config_path().read_text() == original


@pytest.mark.parametrize('service_role', ['web', 'collector'])
@pytest.mark.parametrize('role', [None, 'guest', 'pro', 'admin'])
def test_management_api_enforces_admin_with_real_sessions(actors, monkeypatch, service_role, role):
    from app.main import create_app
    from app.services import collector_proxy

    store, owners = actors
    permissions = defaults(owners)
    permissions['list_ext_tables'] = 'pro'

    async def no_proxy(request, call_next):
        return await call_next(request)

    monkeypatch.setattr(collector_proxy, 'proxy_middleware', no_proxy)

    @asynccontextmanager
    async def lifespan(app):
        app.state.auth_store = store
        yield

    app = create_app(service_role)
    app.router.lifespan_context = lifespan
    with TestClient(app) as client:
        if role:
            client.cookies.set('tf_session', owners[role].session_token)
        for method in ('GET', 'PUT'):
            response = client.request(method, '/api/custom/assistant/tool-permissions',
                                      json={'permissions': permissions} if method == 'PUT' else None)
            if role != 'admin':
                assert response.status_code == (401 if role is None else 403), response.text
                assert not config_path().exists()
                continue
            assert response.status_code == 200, response.text
            assert response.json()['counts'] == {
                'guest': 9, 'pro': 15 + (method == 'PUT'), 'admin': 24,
            }
        if role == 'admin':
            assert client.get('/api/custom/assistant/tool-permissions').json()['counts']['pro'] == 16
            invalid = {**permissions, 'unknown': 'guest'}
            rejected = client.put('/api/custom/assistant/tool-permissions', json={'permissions': invalid})
            assert rejected.status_code == 422
            assert json.loads(config_path().read_text()) == {'permissions': permissions}


def test_unknown_tool_remains_explicitly_rejected(actors):
    _store, owners = actors
    with user_scope(owners['admin']), pytest.raises(AuthError, match='未知工具: sync_data'):
        require_tool_access('sync_data')
