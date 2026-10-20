"""Production frontend files must reach browsers before the SPA fallback."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def frontend_client(tmp_path, monkeypatch):
    from app.config import settings

    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>workbench</title>")
    (static / "assets").mkdir()
    (static / "assets" / "app-test.js").write_text("console.log('workbench')")
    monkeypatch.setattr(settings, "static_dir", static)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    # No lifespan: static requests must not start collection or require a session.
    return TestClient(create_app("web")), static


def test_favicon_returns_svg_instead_of_spa_html(frontend_client):
    client, static = frontend_client
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"/>'
    (static / "favicon.svg").write_text(svg)

    response = client.get("/favicon.svg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.headers["cache-control"] == "no-cache"
    assert response.text == svg
    # A browser reload must see a replaced icon at the same URL.
    updated = svg.replace("32 32", "64 64")
    (static / "favicon.svg").write_text(updated)
    assert client.get("/favicon.svg").text == updated


def test_missing_favicon_returns_404_instead_of_spa_html(frontend_client):
    client, _ = frontend_client

    assert client.get("/favicon.svg").status_code == 404


def test_spa_routes_and_bundled_assets_still_work(frontend_client):
    client, _ = frontend_client

    for path in ("/", "/settings/data-sources"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "no-store" in response.headers["cache-control"]
        assert response.text == "<!doctype html><title>workbench</title>"

    response = client.get("/assets/app-test.js")
    assert response.status_code == 200
    assert response.text == "console.log('workbench')"
