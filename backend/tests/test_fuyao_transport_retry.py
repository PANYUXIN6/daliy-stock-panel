from concurrent.futures import CancelledError
from threading import Event

import httpx
import pytest

from app.plugins.fuyao import client as fc


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(fc, '_wait_request_slot', lambda cancel_event=None: None)
    monkeypatch.setattr(fc.time, 'sleep', lambda seconds: None)
    provider = fc.FuyaoClient('test-key')
    yield provider
    provider.close()


def test_incomplete_body_retries_same_get_and_returns_complete_response(client, monkeypatch):
    calls = []
    def get(path, params):
        calls.append((path, params))
        if len(calls) == 1:
            raise httpx.RemoteProtocolError('incomplete chunked read')
        return httpx.Response(200, json={'code': 0, 'data': {'rows': [{'profit': 100}]}})
    monkeypatch.setattr(client._http, 'get', get)
    assert client._get('/financials', {'symbol': '600000.SH'}) == {'rows': [{'profit': 100}]}
    assert calls == [('/financials', {'symbol': '600000.SH'})] * 2


def test_transport_retry_is_bounded_and_rate_limit_is_not_retried(client, monkeypatch):
    calls = []
    def disconnected(*args, **kwargs):
        calls.append(1)
        raise httpx.ReadError('network disconnected')
    monkeypatch.setattr(client._http, 'get', disconnected)
    with pytest.raises(fc.FuyaoError, match='网络请求失败'):
        client._get('/financials', {})
    assert len(calls) == 3
    calls.clear()
    def limited(*args, **kwargs):
        calls.append(1)
        return httpx.Response(429)
    monkeypatch.setattr(client._http, 'get', limited)
    with pytest.raises(fc.FuyaoRateLimitError):
        client._get('/financials', {})
    assert calls == [1]


def test_cancellation_during_retry_stops_before_another_request(client, monkeypatch):
    cancel = Event()
    calls = []
    def disconnected(*args, **kwargs):
        calls.append(1)
        cancel.set()
        raise httpx.RemoteProtocolError('incomplete chunked read')
    monkeypatch.setattr(client._http, 'get', disconnected)
    with pytest.raises(CancelledError):
        client._get('/financials', {}, cancel_event=cancel)
    assert calls == [1]
