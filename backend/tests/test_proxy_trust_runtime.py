"""Load real Uvicorn configurations before checking application proxy trust."""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml
from click.testing import CliRunner
from fastapi import FastAPI
from starlette.requests import Request
from uvicorn.importer import import_from_string as _import

from app.api.auth import _client_ip, request_origin
from app.config import settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def proxy_probe(monkeypatch):
    observations = []

    async def probe(scope, receive, send):
        request = Request(scope)
        observations.append((request.client.host, _client_ip(request), request_origin(request)))

    def verify(config):
        assert config.proxy_headers is False
        # Even a broadly trusted Uvicorn environment must not rewrite ASGI scope.
        config.forwarded_allow_ips = '*'
        config.load()
        for trusted, expected_ip, expected_origin in [
            ('', '127.0.0.1', 'http://panel'),
            ('192.0.2.1/32', '127.0.0.1', 'http://panel'),
            ('127.0.0.1/32', '203.0.113.77', 'https://panel'),
        ]:
            monkeypatch.setattr(settings, 'auth_trusted_proxies', trusted)
            scope = {
                'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                'method': 'GET', 'scheme': 'http', 'server': ('panel', 80),
                'path': '/', 'raw_path': b'/', 'query_string': b'',
                'client': ('127.0.0.1', 50000),
                'headers': [(b'host', b'panel'), (b'x-forwarded-for', b'203.0.113.77'),
                            (b'x-forwarded-proto', b'https')],
            }
            asyncio.run(config.loaded_app(scope, None, None))
            assert observations[-1] == ('127.0.0.1', expected_ip, expected_origin)

    return probe, verify


@pytest.mark.parametrize('entrypoint', ['docker-web', 'compose-collector'])
def test_container_cli_loads_without_proxy_rewriting(monkeypatch, proxy_probe, entrypoint):
    probe, verify = proxy_probe
    if entrypoint == 'docker-web':
        line = next(line for line in (ROOT / 'Dockerfile').read_text().splitlines() if line.startswith('CMD '))
        command = json.loads(line.removeprefix('CMD '))
    else:
        compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
        command = compose['services']['collector']['command']
    arguments = command[command.index('uvicorn') + 1:]
    # Use the official Click parser and uvicorn.run -> Config construction; only
    # replace the application and socket-serving step, never the middleware loader.
    arguments[0] = 'test_proxy_trust_runtime:probe'
    monkeypatch.setattr('uvicorn.config.import_from_string', lambda value: probe if value == arguments[0] else _import(value))
    calls = []

    def run(server, sockets=None):
        verify(server.config)
        calls.append(True)
        server.started = True

    monkeypatch.setattr(uvicorn.Server, 'run', run)
    result = CliRunner().invoke(uvicorn.main, arguments)
    assert result.exit_code == 0, result.output or repr(result.exception)
    assert calls == [True]


@pytest.mark.parametrize('entrypoint', ['web', 'collector'])
def test_desktop_configs_preserve_raw_peer(monkeypatch, proxy_probe, entrypoint):
    import app.collector
    import app.main
    from app import desktop

    probe, verify = proxy_probe
    monkeypatch.setattr(app.main, 'create_app', lambda role: probe)
    monkeypatch.setattr(app.collector, 'app', probe)
    calls = []

    def run(server, sockets=None):
        verify(server.config)
        calls.append(True)
        server.started = True

    monkeypatch.setattr(uvicorn.Server, 'run', run)
    if entrypoint == 'web':
        ready = threading.Event()
        desktop._run_uvmicorn(3018, ready)
        assert ready.is_set()
    else:
        assert desktop._run_collector_service() == 0
    assert calls == [True]


def test_https_browser_write_uses_collector_transport_origin(tmp_path, monkeypatch):
    from app.services.auth import AuthStore
    from app.services.auth_access import install_auth_middleware
    from app.services.collector_proxy import proxy_middleware

    monkeypatch.setattr(settings, "auth_trusted_proxies", "127.0.0.1/32")
    monkeypatch.setattr(settings, "auth_allowed_origins", "")
    store = AuthStore(tmp_path)
    admin = store.bootstrap("admin", "initial-secret")
    token, _ = store.login("admin", "initial-secret", "test", "")
    writes = []
    collector = FastAPI()
    collector.state.auth_store = store

    @collector.post("/api/pipeline/probe")
    async def write_probe(request: Request):
        writes.append(await request.json())
        return {"owner": request.state.user["id"], "origin": request_origin(request)}

    install_auth_middleware(collector)
    web = FastAPI()
    web.state.service_role = "web"
    web.state.auth_store = store
    web.state.collector_url = "http://collector:3019"
    web.middleware("http")(proxy_middleware)
    install_auth_middleware(web)

    async def exercise():
        transport = httpx.ASGITransport(app=collector, client=("127.0.0.1", 50001))
        async with httpx.AsyncClient(transport=transport) as collector_client:
            web.state.collector_client = collector_client
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=web, client=("127.0.0.1", 50000)),
                base_url="http://panel",
                headers={"Cookie": f"tf_session={token}", "X-Expected-User": admin["id"]},
            ) as browser:
                headers = {
                    "Origin": "https://panel", "X-Forwarded-Proto": "https",
                    "X-Forwarded-For": "203.0.113.77",
                    "Forwarded": "for=203.0.113.77;proto=https",
                }
                response = await browser.post("/api/pipeline/probe", headers=headers, json={"value": 7})
                assert response.status_code == 200, response.text
                assert response.json() == {"owner": admin["id"], "origin": "http://collector:3019"}
                assert writes == [{"value": 7}]

                headers["Origin"] = "https://untrusted.example"
                rejected = await browser.post("/api/pipeline/probe", headers=headers, json={"value": 9})
                assert rejected.status_code == 403
                assert rejected.json()["code"] == "CSRF_REJECTED"
                assert writes == [{"value": 7}]

    asyncio.run(exercise())
