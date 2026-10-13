from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import settings as api
from app.data_providers import custom
from app.services import trading_day


@pytest.mark.parametrize('endpoint,payload', [
    ('/plugin-key', {'plugin': 'fuyao', 'api_key': 'good'}),
    ('/data-sources/reload', None),
    ('/data-sources', {'name': 'example', 'datasets': {}}),
])
def test_source_save_updates_collector_capabilities_scheduler_and_calendar(tmp_path, monkeypatch, endpoint, payload):
    app = FastAPI()
    app.include_router(api.router)
    app.state.datastore = SimpleNamespace(data_dir=tmp_path)
    scheduler = Mock()
    app.state.financial_scheduler = scheduler
    capabilities = object()
    monkeypatch.setattr(api, 'detect_capabilities', lambda **kwargs: capabilities)
    monkeypatch.setattr(api, 'list_data_sources', lambda: {})
    monkeypatch.setattr(custom, 'probe_plugin_key', lambda *args: (True, 'ok'))
    monkeypatch.setattr(api.secrets_store, 'save', lambda *args: None)
    monkeypatch.setattr(custom, 'load_all', lambda: None)
    monkeypatch.setattr(custom, 'list_plugins', lambda: [])
    monkeypatch.setattr(custom, 'create_provider', lambda *args: Mock())
    monkeypatch.setattr(custom, 'save_config', lambda *args: None)
    sync = Mock(return_value={'updated': True})
    monkeypatch.setattr(trading_day, 'sync_calendar', sync)
    response = TestClient(app).post('/api/settings' + endpoint, json=payload)
    assert response.status_code == 200, response.text
    assert app.state.capabilities is capabilities
    scheduler.update_capabilities.assert_called_once_with(capabilities)
    sync.assert_called_once_with(tmp_path)


def test_calendar_sync_failure_is_logged_and_keeps_saved_configuration(tmp_path, monkeypatch, caplog):
    state = SimpleNamespace(datastore=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(api, 'detect_capabilities', lambda **kwargs: 'new')
    monkeypatch.setattr(trading_day, 'sync_calendar', Mock(side_effect=RuntimeError('unavailable')))
    api._refresh_data_source_runtime(SimpleNamespace(app=SimpleNamespace(state=state)))
    assert state.capabilities == 'new'
    assert '交易日历同步失败' in caplog.text
