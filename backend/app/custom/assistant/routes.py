"""AI 助手 HTTP 端点 — 薄层: 参数校验 + 流式响应映射。

POST /api/custom/assistant/chat      NDJSON 事件流(协议见模块 __init__)
GET  /api/custom/assistant/status     供应商配置状态(前端门控展示)
GET  /api/custom/assistant/suggests   空会话快捷指令
GET/PUT /api/custom/assistant/tool-permissions  管理员工具权限配置
POST /api/custom/assistant/actions/{call_id}/decision
                                     动作工具确认/拒绝(确认卡按钮回调)
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.custom.assistant import actions as assistant_actions
from app.custom.assistant import tools as assistant_tools
from app.custom.assistant.chat_service import chat_stream
from app.custom.assistant.permissions import get_tool_permissions, set_tool_permissions
from app.services.ai_provider import (
    ai_configured,
    current_ai_model,
    current_ai_provider,
    is_codex_cli_provider,
)


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)


class AssistantContext(BaseModel):
    page: str = Field(default="", max_length=100)
    symbol: str = Field(default="", max_length=32)


class ChatRequest(BaseModel):
    messages: list[HistoryMessage] = Field(min_length=1, max_length=200)
    context: AssistantContext | None = None


class ActionDecisionRequest(BaseModel):
    approve: bool


class ToolPermissionsRequest(BaseModel):
    model_config = {"extra": "forbid"}
    permissions: dict[str, Literal["guest", "pro", "admin"]]


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/custom/assistant", tags=["custom-assistant"])

    @router.post("/chat")
    async def chat(request: Request, req: ChatRequest):
        from app.services.user_runtime import runtime_for
        engine = runtime_for(request).strategy_engine
        repo = getattr(request.app.state, "repo", None)
        data_dir = str(repo.store.data_dir) if repo is not None else None
        context = req.context.model_dump() if req.context else None
        from app.services.ai_credit_stream import CreditStreamingResponse, prepare_credit_task

        credit_task = await prepare_credit_task(request, "assistant")

        return CreditStreamingResponse(
            chat_stream(
                history=[m.model_dump() for m in req.messages],
                context=context,
                engine=engine,
                data_dir=data_dir,
                repo=repo,
                quote_service=getattr(request.app.state, "quote_service", None),
                depth_service=getattr(request.app.state, "depth_service", None),
                capabilities=getattr(request.app.state, "capabilities", None),
                financial_scheduler=getattr(request.app.state, "financial_scheduler", None),
            ),
            credit_task=credit_task,
        )

    @router.get("/status")
    def status() -> dict:
        configured = ai_configured()
        return {
            "configured": configured,
            "provider": current_ai_provider(),
            "model": current_ai_model(),
            "supports_tools": configured and not is_codex_cli_provider(),
        }

    @router.get("/suggests")
    def suggests() -> dict:
        return {"suggests": assistant_tools.QUICK_SUGGESTS}

    @router.get("/tool-permissions")
    def tool_permissions() -> dict:
        return get_tool_permissions()

    @router.put("/tool-permissions")
    def update_tool_permissions(req: ToolPermissionsRequest) -> dict:
        return set_tool_permissions(req.permissions)

    @router.post("/actions/{call_id}/decision")
    async def action_decision(call_id: str, req: ActionDecisionRequest) -> dict:
        """确认卡回调: 唤醒挂起中的动作工具(批准执行或拒绝)。"""
        action = await assistant_actions.registry.resolve(call_id, req.approve)
        if action is None:
            raise HTTPException(status_code=404, detail="确认请求不存在或已过期")
        return {"ok": True, "call_id": call_id, "decision": "approved" if req.approve else "denied"}

    return router
