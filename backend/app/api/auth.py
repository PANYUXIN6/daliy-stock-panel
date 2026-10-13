"""Account login and fixed-lifetime, HttpOnly browser sessions."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.config import settings
from app.services import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])
COOKIE_NAME = "tf_session"


def store_for(request: Request) -> auth.AuthStore:
    return getattr(request.app.state, "auth_store", None) or auth.get_store()


def _trusted(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
        return any(
            address in ipaddress.ip_network(cidr.strip())
            for cidr in settings.auth_trusted_proxies.split(",")
            if cidr.strip()
        )
    except ValueError:
        return False


def _client_ip(request: Request) -> str:
    direct = request.client.host if request.client else "unknown"
    if not _trusted(direct):
        return direct
    chain = [
        part.strip()
        for part in request.headers.get("x-forwarded-for", "").split(",")
        if part.strip()
    ]
    current = direct
    for part in reversed(chain):
        if not _trusted(current):
            break
        try:
            current = str(ipaddress.ip_address(part))
        except ValueError:
            return direct
    return current


def request_origin(request: Request) -> str:
    scheme = request.url.scheme
    direct = request.client.host if request.client else ""
    if _trusted(direct):
        forwarded = request.headers.get("x-forwarded-proto", "").strip().lower()
        if forwarded in ("http", "https"):
            scheme = forwarded
    return f"{scheme}://{request.url.netloc}"


def same_origin(request: Request) -> bool:
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = request.headers.get("origin")
    if not origin and request.headers.get("referer"):
        parsed = urlsplit(request.headers["referer"])
        origin = f"{parsed.scheme}://{parsed.netloc}"
    # Non-browser clients without ambient cross-site origins remain usable.
    if origin is None:
        return True
    allowed = {
        request_origin(request),
        *(o.strip().rstrip("/") for o in settings.auth_allowed_origins.split(",") if o.strip()),
    }
    return origin.rstrip("/") in allowed


def _cookie(request: Request, response: Response, token: str):
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=auth.SESSION_TTL,
        httponly=True,
        samesite="strict",
        path="/",
        secure=settings.auth_cookie_secure or request_origin(request).startswith("https://"),
    )
    response.headers["Cache-Control"] = "no-store"


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


@router.get("/status")
def auth_status(request: Request, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return store_for(request).status(request.cookies.get(COOKIE_NAME))


@router.post("/login")
def login(req: LoginIn, request: Request, response: Response) -> dict:
    store = store_for(request)
    if not store.configured():
        raise auth.AuthError("请在服务器配置初始管理员账号和密码", 409, "NOT_INITIALIZED")
    token, status = store.login(
        req.username, req.password, _client_ip(request), request.headers.get("user-agent", "")
    )
    _cookie(request, response, token)
    return status


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        store_for(request).revoke_session(token)
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        samesite="strict",
        secure=settings.auth_cookie_secure or request_origin(request).startswith("https://"),
    )
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True}


@router.post("/change-password")
def change_password(req: ChangePasswordIn, request: Request, response: Response) -> dict:
    token, status = store_for(request).change_password(
        request.cookies.get(COOKIE_NAME, ""), req.old_password, req.new_password
    )
    _cookie(request, response, token)
    return status
