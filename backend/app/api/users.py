"""Administrator account management; no private-data impersonation endpoints."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import COOKIE_NAME, store_for
from app.services.ai_credits import get_credit_store
from app.services.auth import AuthError

router = APIRouter(prefix="/api/users", tags=["users"])


def require_admin(request: Request) -> str:
    session = store_for(request).session(request.cookies.get(COOKIE_NAME))
    if not session:
        raise AuthError("未登录或会话已过期", 401)
    user = session["user"]
    if user["role"] != "admin" or user["must_change_password"]:
        raise AuthError("仅管理员可管理账号", 403)
    return user["id"]


class CreateUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    role: Literal["admin", "pro", "guest"] = "guest"
    pro_expires_on: str | None = None


class UpdateUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["admin", "pro", "guest"] | None = None
    pro_expires_on: str | None = None
    is_active: bool | None = None


class ResetPassword(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=8, max_length=128)


@router.get("")
def list_users(
    request: Request,
    search: str = Query("", max_length=64),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    actor: str = Depends(require_admin),
):
    accounts = store_for(request)
    result = accounts.list_users(search, limit, offset)
    usage = get_credit_store(accounts.data_dir).usage_for_users(result["users"])
    for user in result["users"]:
        user["ai_usage"] = usage[user["id"]]
    return result


@router.get("/login-events")
def login_events(
    request: Request,
    user_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    actor: str = Depends(require_admin),
):
    return store_for(request).login_events(user_id, limit, offset)


@router.post("")
def create_user(req: CreateUser, request: Request, actor: str = Depends(require_admin)):
    return {"user": store_for(request).create_user(**req.model_dump(), actor_id=actor)}


@router.patch("/{user_id}")
def update_user(
    user_id: str, req: UpdateUser, request: Request, actor: str = Depends(require_admin)
):
    changes = req.model_dump(exclude_unset=True)
    if any(changes.get(key, True) is None for key in ("role", "is_active")):
        raise AuthError("角色和账号状态不能为空")
    return {"user": store_for(request).update_user(user_id, changes, actor_id=actor)}


@router.post("/{user_id}/reset-password")
def reset_password(
    user_id: str, req: ResetPassword, request: Request, actor: str = Depends(require_admin)
):
    store_for(request).reset_password(user_id, req.password, actor_id=actor)
    return {"ok": True}


@router.post("/{user_id}/revoke-session")
def revoke_session(user_id: str, request: Request, actor: str = Depends(require_admin)):
    store_for(request).revoke_user_session(user_id, actor_id=actor)
    return {"ok": True}
