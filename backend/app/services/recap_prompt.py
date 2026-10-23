"""Per-account recap templates; generation snapshots the exact template used."""
from __future__ import annotations

import hashlib

from app.services import preferences
from app.services.auth import AuthError, get_store, require_current_session
from app.services.user_context import current_user

MAX_CHARS = 32_000
_KEY = "market_recap_system_prompt"


def _allowed() -> bool:
    owner = current_user(required=False)
    if owner is None:
        return False
    user = (require_current_session() if owner.session_token
            else get_store(owner.data_dir).get_user(owner.id))
    return bool(user and user["is_active"] and user["role"] in {"pro", "admin"})


def require_access() -> None:
    if not _allowed():
        raise AuthError("自定义复盘提示词仅限 Pro 和管理员", 403, "PRO_REQUIRED")


def _custom() -> str:
    value = preferences.load().get(_KEY)
    return value.strip() if isinstance(value, str) and len(value) <= MAX_CHARS else ""


def configuration() -> dict:
    from app.services.market_recap import _SYSTEM_PROMPT
    custom = _custom()
    return {"custom": custom, "default": _SYSTEM_PROMPT,
            "customized": bool(custom), "max_chars": MAX_CHARS}


def save(text: str) -> None:
    require_access()
    if len(text) > MAX_CHARS:
        raise ValueError(f"复盘提示词不能超过 {MAX_CHARS:,} 字符")
    # Empty values retain the personal key and track the current built-in default.
    preferences.save({_KEY: text.strip()})


def snapshot() -> dict:
    from app.services.market_recap import _SYSTEM_PROMPT
    custom = _custom() if _allowed() else ""
    text = custom or _SYSTEM_PROMPT
    return {"source": "custom" if custom else "default", "text": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
