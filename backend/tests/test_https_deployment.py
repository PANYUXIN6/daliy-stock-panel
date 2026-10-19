"""Exercise the public HTTPS configuration without a server or certificate authority."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shutil
import subprocess
from http.cookies import SimpleCookie
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI, Request

from app.api import auth as auth_api
from app.config import settings
from app.services.auth import AuthStore
from app.services.auth_access import install_auth_middleware

ROOT = Path(__file__).resolve().parents[2]


class _ComposeLoader(yaml.SafeLoader):
    pass


_ComposeLoader.add_constructor(
    "!override", lambda loader, node: loader.construct_sequence(node)
)


def _https_config():
    return yaml.load((ROOT / "docker-compose.https.yml").read_text(), Loader=_ComposeLoader)


def _default(value):
    return re.sub(r"\$\{[A-Z_]+:-([^}]*)\}", r"\1", str(value))


def test_https_configuration_scopes_proxy_trust_and_preserves_certificate_storage():
    config = _https_config()
    web, caddy = (config["services"][name] for name in ("web", "caddy"))
    proxy_ip = _default(caddy["networks"]["https_proxy"]["ipv4_address"])
    assert [_default(port) for port in web["ports"]] == ["127.0.0.1:3018:3018"]
    assert _default(web["environment"]["AUTH_TRUSTED_PROXIES"]) == f"{proxy_ip}/32"
    assert str(web["environment"]["AUTH_COOKIE_SECURE"]).lower() == "true"
    assert set(web["networks"]) == {"default", "https_proxy"}
    assert set(caddy["networks"]) == {"https_proxy"}
    subnet = config["networks"]["https_proxy"]["ipam"]["config"][0]["subnet"]
    dynamic_range = config["networks"]["https_proxy"]["ipam"]["config"][0]["ip_range"]
    assert ipaddress.ip_address(proxy_ip) in ipaddress.ip_network(_default(subnet))
    assert ipaddress.ip_network(_default(dynamic_range)).subnet_of(ipaddress.ip_network(_default(subnet)))
    assert ipaddress.ip_address(proxy_ip) not in ipaddress.ip_network(_default(dynamic_range))
    assert set(caddy["ports"]) >= {"80:80", "443:443"}
    assert "caddy_data:/data" in caddy["volumes"]
    assert "caddy_config:/config" in caddy["volumes"]
    assert "./deploy/Caddyfile:/etc/caddy/Caddyfile:ro" in caddy["volumes"]
    assert {"caddy_data", "caddy_config"} <= set(config["volumes"])


@pytest.mark.parametrize("custom_network", [False, True], ids=["defaults", "custom-network"])
def test_compose_https_replaces_legacy_public_http_mapping(tmp_path, custom_network):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI is not installed; static HTTPS contracts run separately")
    version = subprocess.run(
        [docker, "compose", "version"], capture_output=True, text=True, timeout=15, check=False
    )
    if version.returncode != 0:
        pytest.skip("Docker Compose CLI is unavailable; no Docker daemon is needed")

    environment = dict(os.environ)
    for key in ("HOST", "PORT", "HTTPS_PROXY_IP", "HTTPS_PROXY_SUBNET", "HTTPS_PROXY_RANGE", "HTTPS_BACKEND_PORT"):
        environment.pop(key, None)
    proxy_ip, proxy_subnet = "172.30.255.2", "172.30.255.0/24"
    dynamic_range = "172.30.255.128/25"
    backend_port = "3018"
    if custom_network:
        proxy_ip, proxy_subnet, backend_port = "172.29.200.2", "172.29.200.0/24", "18018"
        dynamic_range = "172.29.200.128/25"
        environment.update(
            HTTPS_PROXY_IP=proxy_ip,
            HTTPS_PROXY_SUBNET=proxy_subnet,
            HTTPS_PROXY_RANGE=dynamic_range,
            HTTPS_BACKEND_PORT=backend_port,
        )
    (tmp_path / ".env").write_text("HOST=0.0.0.0\nPORT=80\n")
    legacy = tmp_path / "docker-compose.override.yml"
    legacy.write_text("services:\n  web:\n    ports:\n      - '0.0.0.0:80:3018'\n")
    generated = tmp_path / ".deploy-https.json"
    generated.write_text(json.dumps({"services": {"caddy": {
        "environment": {"SITE_DOMAIN": "shuaihub.cn"},
        "volumes": [{
            "type": "bind", "source": str(ROOT / "deploy" / "Caddyfile"),
            "target": "/etc/caddy/Caddyfile", "read_only": True,
        }],
    }}}))
    result = subprocess.run(
        [
            docker, "compose", "--project-directory", str(tmp_path),
            "--env-file", str(tmp_path / ".env"), "-p", "https-config-test",
            "-f", str(ROOT / "docker-compose.yml"), "-f", str(legacy),
            "-f", str(ROOT / "docker-compose.https.yml"), "-f", str(generated),
            "config", "--format", "json",
        ],
        env=environment, capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    web, caddy, collector = (config["services"][name] for name in ("web", "caddy", "collector"))
    assert len(web["ports"]) == 1
    port, = web["ports"]
    assert (port["host_ip"], str(port["published"]), port["target"]) == (
        "127.0.0.1", backend_port, 3018,
    )
    assert not collector.get("ports")
    assert {(str(port["published"]), port["target"]) for port in caddy["ports"]} >= {
        ("80", 80), ("443", 443),
    }
    assert web["environment"]["AUTH_TRUSTED_PROXIES"] == f"{proxy_ip}/32"
    assert set(web["networks"]) == {"default", "https_proxy"}
    assert set(caddy["networks"]) == {"https_proxy"}
    assert caddy["networks"]["https_proxy"]["ipv4_address"] == proxy_ip
    assert "https_proxy" not in collector["networks"]
    assert config["networks"]["https_proxy"]["ipam"]["config"] == [
        {"subnet": proxy_subnet, "ip_range": dynamic_range}
    ]
    assert ipaddress.ip_address(proxy_ip) not in ipaddress.ip_network(dynamic_range)
    mounts = {mount["target"]: mount for mount in caddy["volumes"]}
    assert mounts["/data"]["source"] == "caddy_data"
    assert mounts["/config"]["source"] == "caddy_config"
    assert mounts["/etc/caddy/Caddyfile"]["source"] == str(ROOT / "deploy" / "Caddyfile")
    assert mounts["/etc/caddy/Caddyfile"]["read_only"] is True
    assert caddy["environment"]["SITE_DOMAIN"] == "shuaihub.cn"


def test_https_proxy_login_keeps_secure_session_real_ip_and_same_origin(tmp_path, monkeypatch):
    config = _https_config()
    web = config["services"]["web"]
    proxy_ip = _default(config["services"]["caddy"]["networks"]["https_proxy"]["ipv4_address"])
    monkeypatch.setattr(settings, "auth_trusted_proxies", _default(web["environment"]["AUTH_TRUSTED_PROXIES"]))
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "auth_allowed_origins", "")
    store = AuthStore(tmp_path)
    admin = store.bootstrap("admin", "initial-secret")
    app = FastAPI()
    app.state.auth_store = store
    app.include_router(auth_api.router)
    writes = []

    @app.post("/api/https-probe")
    async def write_probe(request: Request):
        writes.append(await request.json())
        return {"owner": request.state.user["id"]}

    install_auth_middleware(app)

    async def exercise():
        headers = {
            "Origin": "https://shuaihub.cn", "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "203.0.113.77",
        }
        transport = httpx.ASGITransport(app=app, client=(proxy_ip, 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://shuaihub.cn") as client:
            response = await client.post(
                "/api/auth/login", headers=headers,
                json={"username": "admin", "password": "initial-secret"},
            )
            assert response.status_code == 200, response.text
            cookies = SimpleCookie(response.headers["set-cookie"])
            session = cookies[auth_api.COOKIE_NAME]
            assert session["secure"] and session["httponly"]
            assert session["samesite"].lower() == "strict"
            assert AuthStore(tmp_path).get_user(admin["id"])["last_login_ip"] == "203.0.113.77"

            # The browser sends Secure cookies over TLS; Caddy forwards them over internal HTTP.
            authenticated = {**headers, "Cookie": f"{session.key}={session.value}"}
            accepted = await client.post("/api/https-probe", headers=authenticated, json={"value": 7})
            assert accepted.status_code == 200, accepted.text
            assert accepted.json() == {"owner": admin["id"]}
            rejected = await client.post(
                "/api/https-probe", headers={**authenticated, "Origin": "https://untrusted.example"},
                json={"value": 9},
            )
            assert rejected.status_code == 403
            assert rejected.json()["code"] == "CSRF_REJECTED"
            assert writes == [{"value": 7}]

        untrusted = httpx.ASGITransport(app=app, client=("203.0.113.88", 50001))
        async with httpx.AsyncClient(transport=untrusted, base_url="http://shuaihub.cn") as client:
            spoofed = await client.post(
                "/api/auth/login", headers=headers,
                json={"username": "admin", "password": "initial-secret"},
            )
            assert spoofed.status_code == 403
            assert "set-cookie" not in spoofed.headers
            assert AuthStore(tmp_path).get_user(admin["id"])["login_count"] == 1

    asyncio.run(exercise())
