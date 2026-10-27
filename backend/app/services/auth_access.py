"""Fail-closed browser authentication, role gates and request ownership.

Install last so this ASGI middleware wraps the business middleware and streaming
response lifetime. Collector API requests use the same session authority.
"""

from __future__ import annotations

import sqlite3

from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse

from app.api.auth import COOKIE_NAME, same_origin, store_for
from app.services.auth import AuthError
from app.services.user_context import UserContext, user_scope

_PERSONAL_PREFERENCES = frozenset(
    {
        "nav-order",
        "nav-hidden",
        "dashboard-layout",
        "watchlist-columns",
        "screener-result-columns",
        "watchlist-groups-in-nav",
        "realtime-monitor",
    }
)
_PUBLIC = frozenset({"/api/auth/status", "/api/auth/login", "/api/health"})
_AUTH_MUTATIONS = frozenset({"/api/auth/logout", "/api/auth/change-password"})


def admin_required(path: str, method: str) -> bool:
    # 选股叠加条件与信号编辑共用字段元数据，不包含个人信号定义。
    if path == '/api/custom-signals/options' and method in ('GET', 'HEAD'):
        return False
    if any(path == p or path.startswith(p + '/') for p in (
        '/api/monitor-rules', '/api/custom-signals', '/api/alerts', '/api/lots',
    )):
        return True
    # 因子检验、挖掘及候选管理与回测页共用 URL 前缀，保留各自业务权限。
    if ((path == '/api/backtest' or path.startswith('/api/backtest/'))
            and not any(path == p or path.startswith(p + '/') for p in (
            '/api/backtest/factor', '/api/backtest/mining', '/api/backtest/candidates',
        ))):
        return True
    if path == "/api/users" or path.startswith("/api/users/"):
        return True
    if path in {"/api/data/version", "/api/data/status"} and method in ("GET", "HEAD"):
        return False
    if any(path == p or path.startswith(p + "/") for p in ("/api/data", "/api/pipeline")):
        return True
    if method in ("GET", "HEAD", "OPTIONS"):
        if path.startswith("/api/settings"):
            return path not in {
                "/api/settings",
                "/api/settings/preferences",
                "/api/settings/capability-matrix",
                "/api/settings/minute-refresh/status",
                "/api/settings/preferences/watchlist-columns",
                "/api/settings/preferences/screener-result-columns",
                "/api/settings/preferences/quote-interval",
            }
        return path.startswith("/api/ext-data/") and path.endswith("/api-key")
    if path == "/api/backtest/mining/config":
        return True
    # 详情空态只允许显式补采单标的、单日期；批量维护仍限管理员。
    if path == "/api/kline/minute/fetch" and method == "POST":
        return False
    if path.startswith("/api/kline/"):
        return path not in {
            "/api/kline/instruments/names", "/api/kline/daily-batch", "/api/kline/minute-batch",
        }
    if (path in {"/api/index/sync_instruments", "/api/index/sync_daily", "/api/intraday/refresh"}
            or path == "/api/screener/earnings-gap/verify"
            or path.startswith("/api/financials/sync/")
            or path == "/api/analysis-menus" or path.startswith("/api/analysis-menus/")):
        return True
    if path.startswith("/api/settings"):
        return path.removeprefix("/api/settings/preferences/") not in _PERSONAL_PREFERENCES
    # 概念／行业 AI 轮动分析向所有已登录用户开放。
    if path == "/api/rps/rotation-analyze" and method == "POST":
        return False
    return any(
        path == p or path.startswith(p + "/")
        for p in ("/api/ext-data", "/api/regime", "/api/rps", "/api/capabilities")
    )


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        response_started = False
        original_send = send

        async def send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
            await original_send(message)

        request = Request(scope, receive)
        path = scope["path"].rstrip("/")
        try:
            if request.method == "OPTIONS":
                await self.app(scope, receive, send)
                return
            if request.method not in ("GET", "HEAD") and not same_origin(request):
                raise AuthError("请求来源不受信任", 403, "CSRF_REJECTED")
            if (
                path == "/api/events"
                or path.startswith("/api/events/")
                or path.startswith("/api/settings/api-tokens")
            ):
                raise AuthError("外部 API Token 与事件票据已停用", 403, "EXTERNAL_API_DISABLED")
            if path in _PUBLIC:
                await self.app(scope, receive, send)
                return
            store = await run_in_threadpool(store_for, request)
            token = request.cookies.get(COOKIE_NAME)
            session = await run_in_threadpool(store.session, token)
            if not session:
                raise AuthError("未登录或会话已过期", 401, "SESSION_INVALID")
            user = session["user"]
            expected = (request.headers.get("x-expected-user"), request.query_params.get("expected_user_id"))
            if path in _AUTH_MUTATIONS and all(value is None for value in expected):
                raise AuthError("请确认当前登录账号后重试", 401, "SESSION_OWNER_REQUIRED")
            if any(value is not None and value != user["id"] for value in expected):
                raise AuthError("登录账号已变更，请重新载入", 401, "SESSION_OWNER_CHANGED")
            if user["must_change_password"] and path not in _AUTH_MUTATIONS:
                raise AuthError("请先修改临时密码", 403, "PASSWORD_CHANGE_REQUIRED")
            if admin_required(path, request.method) and user["role"] != "admin":
                raise AuthError("此操作仅限管理员", 403, "ADMIN_REQUIRED")
            scope.setdefault("state", {})["user"] = user
            context = UserContext(
                user["id"],
                user["role"],
                store.data_dir,
                user["is_active"],
                token,
                session["expires_at"],
            )
            with user_scope(context):
                await self.app(scope, receive, send)
        except AuthError as exc:
            if response_started:
                raise
            await JSONResponse(
                {"detail": exc.detail, "code": exc.code},
                status_code=exc.status,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
        except sqlite3.Error:
            if response_started:
                raise
            await JSONResponse(
                {"detail": "身份服务暂不可用", "code": "AUTH_UNAVAILABLE"}, status_code=503
            )(scope, receive, send)


def install_auth_middleware(app):
    async def handle_auth_error(request, exc):
        return JSONResponse(
            {"detail": exc.detail, "code": exc.code},
            status_code=exc.status,
            headers={"Cache-Control": "no-store"},
        )

    app.add_exception_handler(AuthError, handle_auth_error)
    app.add_middleware(AuthMiddleware)
