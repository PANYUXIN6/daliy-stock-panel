from __future__ import annotations

import pytest
from test_strategy_code_save import _code
from test_strategy_code_save import code_client as _code_client

from app.api.strategy import _set_meta_bool_field
from app.strategy.ai_generator import AIStrategyGenerator
from app.strategy.engine import StrategyEngine

code_client = _code_client


@pytest.mark.parametrize('research_only', [False, True])
def test_web_publish_cannot_modify_server_template(code_client, tmp_path, research_only):
    directory = tmp_path / 'strategies' / 'templates'
    directory.mkdir(parents=True)
    target = directory / 'ai_trusted.py'
    original = _set_meta_bool_field(_code('ai_trusted'), 'research_only', research_only)
    target.write_text(original)
    response = code_client.post('/api/strategies/ai_trusted/publish')
    assert response.status_code == 403
    assert 'Python' in response.json()['detail']
    assert target.read_text() == original
    loaded = StrategyEngine(strategy_dirs=[directory]).get('ai_trusted')
    assert loaded.meta['research_only'] is research_only


def test_set_meta_bool_field_insert_then_replace():
    code = 'META = {\n    "id": "x",\n}\n'

    inserted = _set_meta_bool_field(code, "research_only", True)
    assert '"research_only": True' in inserted
    assert AIStrategyGenerator._extract_meta(inserted)["research_only"] is True

    replaced = _set_meta_bool_field(inserted, "research_only", False)
    assert '"research_only": False' in replaced
    assert '"research_only": True' not in replaced
    assert AIStrategyGenerator._extract_meta(replaced)["research_only"] is False
