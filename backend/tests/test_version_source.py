"""服务器版本从 frontend/package.json 加载，缺失时保留启动兜底。"""

import json
from pathlib import Path

import app as app_pkg
from app import __version__

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_JSON = REPO_ROOT / "frontend" / "package.json"


def test_dev_version_matches_package_json():
    """dev 模式: __version__ 与 frontend/package.json 完全一致。"""
    assert __version__ == json.loads(PKG_JSON.read_text(encoding="utf-8"))["version"]


def test_missing_version_metadata_falls_back(monkeypatch, tmp_path):
    """源码/容器缺少版本元数据时不阻塞启动。"""
    monkeypatch.setattr(app_pkg, "__file__", str(tmp_path / "backend" / "app" / "__init__.py"))
    assert app_pkg._load_version() == app_pkg._FALLBACK_VERSION
