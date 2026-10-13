"""Paths for existing personal file formats, with explicit owner and bounded names."""
from __future__ import annotations

import re
from pathlib import Path

from app.services.user_context import user_data_dir


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('无效个人对象标识')
    return value


def private_path(data_dir: Path | None, *parts: str) -> Path:
    root = user_data_dir(data_dir)
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError('个人数据路径超出当前用户目录')
    return path
