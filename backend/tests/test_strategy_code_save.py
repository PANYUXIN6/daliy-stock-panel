from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.api.strategy import (
    StrategyCodeValidateRequest,
    _prepare_strategy_code,
)
from app.strategy.engine import StrategyEngine


def _code(strategy_id: str, name: str = "测试策略") -> str:
    return f'''"""测试策略"""
import polars as pl

META = {{
    "id": "{strategy_id}",
    "name": "{name}",
    "description": "测试描述",
    "tags": ["测试"],
    "params": [],
    "scoring": {{}},
}}

ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 20

RULES = """
1. 测试规则一
2. 测试规则二
3. 测试规则三
"""

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''


def test_prepare_strategy_code_rejects_forbidden_import():
    req = StrategyCodeValidateRequest(
        strategy_id="custom_bad",
        code='''import os\nMETA = {"id": "custom_bad"}\n''',
    )

    with pytest.raises(ValueError, match="禁止 import os"):
        _prepare_strategy_code(req)


def test_prepare_strategy_code_rejects_unknown_scoring_field():
    req = StrategyCodeValidateRequest(
        strategy_id="custom_bad_score",
        code=_code("custom_bad_score").replace(
            '"scoring": {},',
            '"scoring": {"volume_surge": 1.0},',
        ),
    )

    with pytest.raises(ValueError, match="volume_surge"):
        _prepare_strategy_code(req)


@pytest.fixture(params=['admin', 'guest', 'pro'])
def code_client(request, tmp_path):
    from fastapi.testclient import TestClient

    from app.api.auth import COOKIE_NAME
    from app.main import create_app
    from app.services.auth import AuthStore

    store = AuthStore(tmp_path)
    store.bootstrap('admin', 'initial-secret')
    role = request.param
    username = 'admin'
    password = 'initial-secret'
    if role != 'admin':
        username = role
        store.create_user(username, 'temporary-secret', role, '2099-12-31' if role == 'pro' else None)
        token, _ = store.login(username, 'temporary-secret', 'test', '')
        store.change_password(token, 'temporary-secret', 'changed-secret')
        password = 'changed-secret'
    token, _ = store.login(username, password, 'test', '')

    @asynccontextmanager
    async def lifespan(app):
        app.state.auth_store = store
        yield

    app = create_app('collector')
    app.router.lifespan_context = lifespan
    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, token)
        yield client


@pytest.mark.parametrize('source,mode', [('ai', 'create'), ('custom', 'create'), ('custom', 'update')])
def test_web_code_save_rejected_for_every_role(code_client, tmp_path, source, mode):
    target = tmp_path / 'strategies' / 'templates' / 'custom_saved.py'
    target.parent.mkdir(parents=True)
    original = _code('custom_saved', '服务器可信模板')
    target.write_text(original)
    response = code_client.post('/api/strategies/code/save', json={
        'strategy_id': 'custom_saved', 'target_source': source, 'mode': mode,
        'code': _code('custom_saved', '网页替换'),
    })
    assert response.status_code == 403
    assert 'Python' in response.json()['detail']
    assert target.read_text() == original
    assert not (tmp_path / 'users').exists()


@pytest.mark.parametrize('path', ['/api/strategies/code/validate', '/api/strategies/ai/save'])
def test_other_web_python_entrypoints_rejected(code_client, path):
    response = code_client.post(path, json={'strategy_id': 'custom_test', 'code': _code('custom_test')})
    assert response.status_code == 403
    assert 'Python' in response.json()['detail']


def test_server_maintained_template_remains_loadable(tmp_path):
    import polars as pl

    directory = tmp_path / 'strategies' / 'templates'
    directory.mkdir(parents=True)
    target = directory / 'custom_trusted.py'
    target.write_text(_code('custom_trusted', '服务器可信模板'))
    engine = StrategyEngine(strategy_dirs=[directory])
    strategy = engine.get('custom_trusted')
    assert strategy.file_path == target
    assert strategy.meta['name'] == '服务器可信模板'
    assert pl.DataFrame({'close': [10.0]}).filter(strategy.filter_fn(pl.DataFrame(), {})).height == 1
