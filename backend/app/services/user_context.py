"""Immutable ownership propagated explicitly to tasks and through HTTP context."""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class UserContext:
    id: str
    role: str
    data_dir: Path
    is_active: bool = True
    session_token: str | None = field(default=None, repr=False)
    expires_at: str | None = None


_current: ContextVar[UserContext | None] = ContextVar("current_user", default=None)


def current_user(required: bool = True) -> UserContext | None:
    user = _current.get()
    if required and user is None:
        raise RuntimeError("个人数据操作缺少用户身份")
    return user


@contextmanager
def user_scope(user: UserContext):
    token = _current.set(user)
    try:
        yield user
    finally:
        _current.reset(token)


def user_data_dir(data_dir: Path | None = None, user_id: str | None = None) -> Path:
    user = current_user(required=user_id is None or data_dir is None)
    root = Path(data_dir if data_dir is not None else user.data_dir)
    uid = user_id if user_id is not None else user.id
    if not re.fullmatch(r"[a-f0-9]{32}", uid):
        raise ValueError("无效用户标识")
    parent = root.resolve() / "users"
    if parent.resolve() != parent:
        raise ValueError("用户根目录不能通过符号链接重定向")
    path = parent / uid
    if path.resolve() != path:
        raise ValueError("用户目录不能通过符号链接重定向")
    return path
