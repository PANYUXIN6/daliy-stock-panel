"""当前账号 AI 额度与管理员运行规则；价格只由服务端决定。"""
from __future__ import annotations

import json

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.ai_credit_policy import CreditPolicy
from app.services.ai_credits import CreditError, get_credit_store
from app.services.ai_metering import credit_configuration_reason
from app.services.auth import AuthError, require_current_session
from app.services.user_context import current_user

router = APIRouter(prefix="/api/ai/credits", tags=["ai-credits"])


def _admin():
    if require_current_session()["role"] != "admin":
        raise AuthError("此操作仅限管理员", 403, "ADMIN_REQUIRED")
    return current_user()


@router.get("")
def credits():
    owner = current_user()
    status = get_credit_store(owner.data_dir).status(owner)
    reason = None if status["unlimited"] else credit_configuration_reason()
    if reason:
        for feature in status["features"].values():
            feature.update(available=False, reason=reason)
    return status


@router.get("/policy")
def policy():
    owner = _admin()
    return get_credit_store(owner.data_dir).get_policy()


class UpdatePolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_version: int = Field(ge=1, strict=True)
    policy: dict


@router.put("/policy")
def update_policy(req: UpdatePolicyRequest):
    owner = _admin()
    try:
        validated = CreditPolicy.model_validate_json(json.dumps(req.policy))
    except ValidationError as exc:
        raise CreditError("无效的 AI 点数或费用规则", 422, "AI_POLICY_INVALID") from exc
    return get_credit_store(owner.data_dir).update_policy(owner, validated, req.policy_version)
