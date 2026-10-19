"""SQLite account authority. No legacy shared-password credentials are imported.

Connections are short-lived: session revocation and account changes are visible to
all web/collector processes without an in-memory credential cache.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

SESSION_TTL = 7 * 24 * 3600
_BEIJING = ZoneInfo("Asia/Shanghai")
_ITERATIONS = 600_000
# Used only when seeding an empty account database, never as a login fallback.
_DEFAULT_ADMIN_USERNAME = "pyxadmin"
_DEFAULT_ADMIN_PASSWORD_HASH = (
    "600000$500df268429d7b07161b214768f184c1$"
    "8336ee09a45f471138b81e630e03952841fea51b7285f4df8da7391db225491b"
)


class AuthError(ValueError):
    def __init__(self, detail: str, status: int = 400, code: str = "AUTH_ERROR"):
        super().__init__(detail)
        self.detail, self.status, self.code = detail, status, code


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _password_hash(password: str) -> str:
    if not 8 <= len(password) <= 128:
        raise AuthError("密码须为 8 至 128 位")
    salt = secrets.token_bytes(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{_ITERATIONS}${salt.hex()}${hashed.hex()}"


def _verify(password: str, encoded: str) -> bool:
    iterations, salt, expected = encoded.split("$")
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(hashed.hex(), expected)


def _username(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-.]{3,64}", value):
        raise AuthError("用户名须为 3 至 64 位字母、数字、下划线、点或短横线")
    return value


def _role(role: str, expires: str | None) -> tuple[str, str | None]:
    if role == "admin":
        raise AuthError("管理员账号唯一，不可新增或授予管理员权限", 409)
    if role not in ("pro", "guest"):
        raise AuthError("无效角色")
    if role != "pro":
        return role, None
    try:
        parsed = date.fromisoformat(expires or "")
        if parsed.isoformat() != expires:
            raise ValueError
    except ValueError:
        raise AuthError("Pro 账号必须设置有效到期日（YYYY-MM-DD）") from None
    return role, expires


def _public(row, now: float | None = None) -> dict:
    user = {
        key: row[key]
        for key in (
            "id",
            "username",
            "role",
            "pro_expires_on",
            "is_active",
            "is_initial_admin",
            "must_change_password",
            "created_at",
            "last_login_at",
            "last_login_ip",
            "login_count",
        )
    }
    user["is_active"] = bool(user["is_active"])
    user["is_initial_admin"] = bool(user["is_initial_admin"])
    user["must_change_password"] = bool(user["must_change_password"])
    today = datetime.fromtimestamp(time.time() if now is None else now, _BEIJING).date()
    if user["role"] == "pro" and date.fromisoformat(user["pro_expires_on"]) < today:
        user["role"] = "guest"
    return user


class AuthStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "auth" / "auth.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE NOT NULL,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL,
                    pro_expires_on TEXT, is_active INTEGER NOT NULL DEFAULT 1,
                    is_initial_admin INTEGER NOT NULL DEFAULT 0,
                    must_change_password INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                    last_login_at TEXT, last_login_ip TEXT, login_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    user_id TEXT PRIMARY KEY REFERENCES users(id),
                    digest TEXT UNIQUE NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_events (
                    id INTEGER PRIMARY KEY, user_id TEXT, username TEXT NOT NULL,
                    occurred REAL NOT NULL, ip TEXT NOT NULL, user_agent TEXT NOT NULL,
                    success INTEGER NOT NULL, reason TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS event_time ON login_events(occurred);
                CREATE TABLE IF NOT EXISTS login_limits (
                    ip TEXT PRIMARY KEY, failures INTEGER NOT NULL, until REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def _connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _admin(db, actor_id):
        if actor_id is None:  # server-owned maintenance calls have no browser actor
            return
        actor = db.execute("SELECT * FROM users WHERE id=?", (actor_id,)).fetchone()
        if (
            not actor
            or not actor["is_active"]
            or actor["role"] != "admin"
            or actor["must_change_password"]
        ):
            raise AuthError("仅管理员可管理账号", 403)

    @staticmethod
    def _managed_user(db, uid):
        row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise AuthError("账号不存在", 404)
        if row["role"] == "admin":
            raise AuthError("管理员账号受保护，不可修改权限、禁用、重置密码或强制下线", 409)
        return row

    def bootstrap(self, username: str, password: str) -> dict | None:
        with self._connection() as db:
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                return None
        username, hashed = _username(username), _password_hash(password)
        return self._bootstrap_hashed(username, hashed)

    def bootstrap_default(self) -> dict | None:
        return self._bootstrap_hashed(_DEFAULT_ADMIN_USERNAME, _DEFAULT_ADMIN_PASSWORD_HASH)

    def _bootstrap_hashed(self, username: str, hashed: str) -> dict | None:
        with self._connection(True) as db:
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                return None
            uid = uuid.uuid4().hex
            db.execute(
                "INSERT INTO users(id,username,password_hash,role,must_change_password,is_initial_admin,created_at) VALUES(?,?,?,?,0,1,?)",
                (uid, username, hashed, "admin", _iso(time.time())),
            )
            db.execute("INSERT INTO metadata(key,value) VALUES('first_admin_id',?)", (uid,))
            return _public(db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def configured(self) -> bool:
        with self._connection() as db:
            return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def first_admin_id(self) -> str | None:
        with self._connection() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='first_admin_id'").fetchone()
            return row[0] if row else None

    def get_user(self, user_id: str, *, now: float | None = None) -> dict | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return _public(row, now) if row else None

    def list_active_users(self) -> list[dict]:
        with self._connection() as db:
            return [_public(row) for row in db.execute("SELECT * FROM users WHERE is_active=1")]

    def list_users(self, search="", limit=50, offset=0) -> dict:
        with self._connection() as db:
            pattern = "%" + search + "%"
            total = db.execute(
                "SELECT COUNT(*) FROM users WHERE username LIKE ?", (pattern,)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM users WHERE username LIKE ? ORDER BY created_at,id LIMIT ? OFFSET ?",
                (pattern, limit, offset),
            )
            return {"users": [_public(r) for r in rows], "total": total}

    def create_user(self, username, password, role="guest", pro_expires_on=None, *, actor_id=None):
        username, hashed = _username(username), _password_hash(password)
        role, expires = _role(role, pro_expires_on)
        with self._connection(True) as db:
            self._admin(db, actor_id)
            uid = uuid.uuid4().hex
            try:
                db.execute(
                    "INSERT INTO users(id,username,password_hash,role,pro_expires_on,created_at) VALUES(?,?,?,?,?,?)",
                    (uid, username, hashed, role, expires, _iso(time.time())),
                )
            except sqlite3.IntegrityError:
                raise AuthError("用户名已存在", 409) from None
            return _public(db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def update_user(self, uid, changes, *, actor_id=None):
        with self._connection(True) as db:
            self._admin(db, actor_id)
            row = self._managed_user(db, uid)
            role, expires = _role(
                changes.get("role", row["role"]),
                changes.get("pro_expires_on", row["pro_expires_on"]),
            )
            active = changes.get("is_active", bool(row["is_active"]))
            db.execute(
                "UPDATE users SET role=?,pro_expires_on=?,is_active=? WHERE id=?",
                (role, expires, active, uid),
            )
            if not active:
                db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            return _public(db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def reset_password(self, uid, password, *, actor_id=None):
        hashed = _password_hash(password)
        with self._connection(True) as db:
            self._admin(db, actor_id)
            self._managed_user(db, uid)
            db.execute(
                "UPDATE users SET password_hash=?,must_change_password=1 WHERE id=?", (hashed, uid)
            )
            db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))

    @staticmethod
    def _issue(db, uid, now):
        token = secrets.token_urlsafe(32)
        db.execute(
            "INSERT OR REPLACE INTO sessions(user_id,digest,expires) VALUES(?,?,?)",
            (uid, hashlib.sha256(token.encode()).hexdigest(), now + SESSION_TTL),
        )
        return token

    def login(self, username, password, ip, user_agent):
        now = time.time()
        error = None
        with self._connection(True) as db:
            db.execute("DELETE FROM login_events WHERE occurred<?", (now - 90 * 86400,))
            db.execute("DELETE FROM login_limits WHERE until<?", (now,))
            limit = db.execute("SELECT * FROM login_limits WHERE ip=?", (ip,)).fetchone()
            row = db.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
            if limit and limit["until"] > now and limit["failures"] >= 5:
                error = AuthError("登录失败次数过多，请稍后重试", 429, "RATE_LIMITED")
                reason = "rate_limited"
            elif not row or not _verify(password, row["password_hash"]) or not row["is_active"]:
                # Unknown accounts do equivalent hash work without persisting credentials.
                if not row:
                    hashlib.pbkdf2_hmac(
                        "sha256", password.encode(), b"unknown-account!", _ITERATIONS
                    )
                failures = (limit["failures"] if limit and limit["until"] > now else 0) + 1
                db.execute(
                    "INSERT OR REPLACE INTO login_limits VALUES(?,?,?)", (ip, failures, now + 300)
                )
                error = AuthError("用户名或密码错误，或账号已禁用", 401, "INVALID_CREDENTIALS")
                reason = "invalid_credentials"
            else:
                token = self._issue(db, row["id"], now)
                db.execute("DELETE FROM login_limits WHERE ip=?", (ip,))
                db.execute(
                    "UPDATE users SET login_count=login_count+1,last_login_at=?,last_login_ip=? WHERE id=?",
                    (_iso(now), ip, row["id"]),
                )
                reason = "success"
            db.execute(
                "INSERT INTO login_events(user_id,username,occurred,ip,user_agent,success,reason) VALUES(?,?,?,?,?,?,?)",
                (
                    row["id"] if row else None,
                    username[:64],
                    now,
                    ip[:128],
                    user_agent[:512],
                    int(error is None),
                    reason,
                ),
            )
        if error:
            raise error
        return token, self.status(token)

    def session(self, token: str | None, *, now=None) -> dict | None:
        if not token or len(token) > 256:
            return None
        now = time.time() if now is None else now
        with self._connection() as db:
            row = db.execute(
                "SELECT u.*,s.expires FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.digest=? AND s.expires>? AND u.is_active=1",
                (hashlib.sha256(token.encode()).hexdigest(), now),
            ).fetchone()
            return {"user": _public(row, now), "expires_at": _iso(row["expires"])} if row else None

    def status(self, token=None):
        session = self.session(token)
        return {
            "configured": self.configured(),
            "authenticated": bool(session),
            "user": session["user"] if session else None,
            "expires_at": session["expires_at"] if session else None,
        }

    def revoke_session(self, token):
        with self._connection(True) as db:
            db.execute(
                "DELETE FROM sessions WHERE digest=?", (hashlib.sha256(token.encode()).hexdigest(),)
            )

    def revoke_user_session(self, uid, *, actor_id=None):
        with self._connection(True) as db:
            self._admin(db, actor_id)
            self._managed_user(db, uid)
            db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))

    def change_password(self, token, old_password, new_password):
        hashed = _password_hash(new_password)
        now = time.time()
        with self._connection(True) as db:
            row = db.execute(
                "SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.digest=? AND s.expires>? AND u.is_active=1",
                (hashlib.sha256(token.encode()).hexdigest(), now),
            ).fetchone()
            if not row:
                raise AuthError("未登录或会话已过期", 401)
            if not _verify(old_password, row["password_hash"]):
                raise AuthError("旧密码错误", 400)
            if _verify(new_password, row["password_hash"]):
                raise AuthError("新密码不能与旧密码相同")
            db.execute(
                "UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?",
                (hashed, row["id"]),
            )
            new_token = self._issue(db, row["id"], now)
        return new_token, self.status(new_token)

    def login_events(self, user_id=None, limit=50, offset=0):
        with self._connection(True) as db:
            db.execute("DELETE FROM login_events WHERE occurred<?", (time.time() - 90 * 86400,))
            where, args = ("WHERE user_id=?", [user_id]) if user_id else ("", [])
            total = db.execute(f"SELECT COUNT(*) FROM login_events {where}", args).fetchone()[0]
            events = []
            for row in db.execute(
                f"SELECT * FROM login_events {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ):
                event = dict(row)
                event["occurred_at"] = _iso(event.pop("occurred"))
                event["success"] = bool(event["success"])
                events.append(event)
            return {"events": events, "total": total}


@lru_cache(maxsize=8)
def _store_at(data_dir: Path) -> AuthStore:
    return AuthStore(data_dir)


def get_store(data_dir: Path | None = None) -> AuthStore:
    from app.config import settings

    return _store_at(Path(data_dir) if data_dir is not None else settings.data_dir)


def bootstrap_from_env() -> bool:
    from app.config import _ENV_FILE, settings

    store = get_store()
    if store.configured():
        return False
    username, password = settings.auth_admin_username, settings.auth_admin_password
    if _ENV_FILE.is_file():
        from dotenv import dotenv_values

        values = {key.upper(): value for key, value in dotenv_values(_ENV_FILE, interpolate=False).items()}
        # Match Settings' case-insensitive environment priority, including explicit
        # empty values, while preserving literal ${...} passwords from .env.
        environment = {key.upper(): value for key, value in os.environ.items()}
        username = environment.get("AUTH_ADMIN_USERNAME", values.get("AUTH_ADMIN_USERNAME") or username)
        password = environment.get("AUTH_ADMIN_PASSWORD", values.get("AUTH_ADMIN_PASSWORD") or password)
    if not username and not password:
        return store.bootstrap_default() is not None
    if not username or not password:
        raise AuthError("自定义初始管理员须同时配置 AUTH_ADMIN_USERNAME 和 AUTH_ADMIN_PASSWORD")
    return store.bootstrap(username, password) is not None


def is_configured() -> bool:
    return get_store().configured()


def is_valid_session(token: str) -> bool:
    return get_store().session(token) is not None


def require_current_session() -> dict:
    """Recheck identity before delayed user actions; captured contexts aren't credentials."""
    from app.services.user_context import current_user
    owner = current_user(required=False)
    if owner is None or not owner.session_token:
        raise AuthError("未登录或会话已过期", 401, "SESSION_INVALID")
    session = get_store(owner.data_dir).session(owner.session_token)
    if session is None or session["user"]["id"] != owner.id:
        raise AuthError("未登录或会话已过期", 401, "SESSION_INVALID")
    if session["user"]["must_change_password"]:
        raise AuthError("请先修改临时密码", 403, "PASSWORD_CHANGE_REQUIRED")
    return session["user"]
