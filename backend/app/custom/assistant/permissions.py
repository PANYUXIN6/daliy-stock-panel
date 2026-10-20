"""AI 助手工具的角色策略与共享配置；每次检查都读取当前会话和配置。"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.config import settings
from app.services.auth import AuthError, require_current_session
from app.services.fs_utils import atomic_write_text

Role = Literal["guest", "pro", "admin"]
_ROLE_LEVEL = {"guest": 0, "pro": 1, "admin": 2}
_lock = threading.RLock()
_CONFIGURATION_ERROR = "AI 助手工具权限配置无法读取，普通账号的工具已停用；请恢复默认设置或重新保存。"


@dataclass(frozen=True)
class ToolPermission:
    name: str
    label: str
    description: str
    group: str
    default_min_role: Role
    locked: bool = False
    requires_confirmation: bool = False


_TOOLS = (
    ToolPermission("get_stock_quote", "实时行情", "查询标的最新价、涨跌幅及成交等行情快照。", "行情与市场", "guest"),
    ToolPermission("get_stock_daily", "日线行情", "查询个股最近交易日的日线、成交与均线数据。", "行情与市场", "guest"),
    ToolPermission("get_market_overview", "市场总览", "查询指数、市场宽度、成交、涨停梯队与情绪概览。", "行情与市场", "guest"),
    ToolPermission("get_indices", "指数行情", "查询核心指数的实时点位与涨跌幅。", "行情与市场", "guest"),
    ToolPermission("get_sector_rotation", "板块轮动", "查询概念或行业活跃榜、切换事件与资金流向。", "行情与市场", "guest"),
    ToolPermission("get_regime", "市场环境", "查询市场强弱、情绪分及周期状态的近期变化。", "行情与市场", "guest"),
    ToolPermission("get_abnormal", "异动监控", "查询盘中异动与交易所异动规则偏离情况。", "行情与市场", "guest"),
    ToolPermission("get_watchlist", "我的自选", "查询当前账号的自选、备注与最新行情。", "我的与个股", "guest"),
    ToolPermission("add_to_watchlist", "加入自选", "经确认后把标的加入当前账号的自选列表。", "我的与个股", "guest", requires_confirmation=True),
    ToolPermission("get_stock_analysis", "个股技术分析", "依据本地日线计算支撑、压力等关键价位与摘要。", "我的与个股", "pro"),
    ToolPermission("get_financials", "财务查询", "查询本地已同步的个股财务报表与核心指标。", "我的与个股", "pro"),
    ToolPermission("list_strategies", "策略目录", "查询当前账号可用策略的标识、参数与说明。", "策略与因子", "pro"),
    ToolPermission("run_strategy", "执行选股策略", "运行策略并返回命中标的；财务依赖可触发按需采集。", "策略与因子", "pro"),
    ToolPermission("list_factors", "因子目录", "查询可用因子的定义、分类与适用范围。", "策略与因子", "pro"),
    ToolPermission("get_factor_values", "因子排名", "查询最新交易日已物化因子的市场取值排名。", "策略与因子", "pro"),
    ToolPermission("list_data_capabilities", "数据能力目录", "查询数据源能力目录及能力说明。", "数据与扩展", "admin"),
    ToolPermission("check_data_coverage", "数据完整性", "检查本地日线、指标、分钟与财务覆盖及同步状态。", "数据与扩展", "admin"),
    ToolPermission("list_ext_tables", "扩展表目录", "列出共享扩展数据表的字段、模式与数据日期。", "数据与扩展", "admin"),
    ToolPermission("query_ext_table", "查询扩展表", "按日期、过滤与排序读取共享扩展表的数据行。", "数据与扩展", "admin"),
    ToolPermission("get_sync_status", "同步任务状态", "查询共享数据同步任务进度、近期任务与财务同步状态。", "数据与扩展", "admin"),
    ToolPermission("run_backtest", "运行回测", "经确认后启动策略回测并返回收益和风险统计。", "管理员功能", "admin", locked=True, requires_confirmation=True),
    ToolPermission("list_signals", "信号目录", "查询当前账号自定义信号的定义与启用状态。", "管理员功能", "admin", locked=True),
    ToolPermission("create_signal_strategy", "创建信号", "经确认后写入自定义信号并刷新相关缓存。", "管理员功能", "admin", locked=True, requires_confirmation=True),
    ToolPermission("get_lots", "持仓提醒", "查询当前账号持仓提醒的数量、成本及目标和止损。", "管理员功能", "admin", locked=True),
)
_BY_NAME = {tool.name: tool for tool in _TOOLS}


def _path() -> Path:
    return settings.data_dir / "user_data" / "assistant_tool_permissions.json"


def _defaults() -> dict[str, str]:
    return {tool.name: tool.default_min_role for tool in _TOOLS}


def _validate(permissions: object, *, loading: bool = False) -> dict[str, str]:
    if not isinstance(permissions, dict) or set(permissions) != set(_BY_NAME):
        raise ValueError("工具权限必须完整包含现有的 24 个工具，不能缺失或包含未知工具")
    result = {}
    for name, tool in _BY_NAME.items():
        role = permissions[name]
        if tool.locked and loading:
            result[name] = "admin"
            continue
        if not isinstance(role, str) or role not in _ROLE_LEVEL:
            raise ValueError("最低角色必须是 guest、pro 或 admin")
        if tool.locked:
            if role != "admin" and not loading:
                raise ValueError("回测、信号定义与持仓提醒只能由管理员使用")
            role = "admin"
        result[name] = role
    return result


def _load() -> tuple[dict[str, str], str | None]:
    try:
        saved = json.loads(_path().read_text(encoding="utf-8"))
        if not isinstance(saved, dict) or set(saved) != {"permissions"}:
            raise ValueError("无效配置结构")
        return _validate(saved["permissions"], loading=True), None
    except FileNotFoundError:
        return _defaults(), None
    except (OSError, UnicodeError, ValueError, TypeError):
        # 已撤销权限的文件损坏时不能恢复默认授权；管理员保留修复入口。
        return dict.fromkeys(_BY_NAME, "admin"), _CONFIGURATION_ERROR


def _require_admin() -> None:
    if require_current_session()["role"] != "admin":
        raise AuthError("此操作仅限管理员", 403, "ADMIN_REQUIRED")


def _response(permissions: dict[str, str], error: str | None) -> dict:
    return {
        "tools": [
            {
                "name": tool.name,
                "label": tool.label,
                "description": tool.description,
                "group": tool.group,
                "min_role": permissions[tool.name],
                "default_min_role": tool.default_min_role,
                "locked": tool.locked,
                "requires_confirmation": tool.requires_confirmation,
            }
            for tool in _TOOLS
        ],
        "counts": {
            role: sum(_ROLE_LEVEL[permissions[tool.name]] <= level for tool in _TOOLS)
            for role, level in _ROLE_LEVEL.items()
        },
        "configuration_error": error,
    }


def get_tool_permissions() -> dict:
    """管理员读取全部工具策略；坏配置提供可修复的保守视图。"""
    _require_admin()
    with _lock:
        permissions, error = _load()
        return _response(permissions, error)


def set_tool_permissions(permissions: dict[str, str]) -> dict:
    """管理员完整替换策略，锁定工具不允许降级。"""
    _require_admin()
    try:
        validated = _validate(permissions)
    except ValueError as exc:
        raise AuthError(str(exc), 422, "TOOL_PERMISSIONS_INVALID") from exc
    with _lock:
        # 等待其它配置写入后重验，避免已撤销的会话继续保存。
        _require_admin()
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"permissions": validated}, ensure_ascii=False, indent=2))
        return _response(validated, None)


def allowed_tool_names() -> frozenset[str]:
    """读取最新会话角色与策略，无缓存；管理员始终拥有全部工具。"""
    role = require_current_session()["role"]
    if role == "admin":
        return frozenset(_BY_NAME)
    with _lock:
        permissions, _error = _load()
    level = _ROLE_LEVEL.get(role, -1)
    return frozenset(name for name, required in permissions.items() if level >= _ROLE_LEVEL[required])


def require_tool_access(name: str) -> None:
    """模型提出工具调用后重新授权；未知工具也拒绝。"""
    role = require_current_session()["role"]
    if name not in _BY_NAME:
        raise AuthError(f"未知工具: {name}", 403, "TOOL_ACCESS_DENIED")
    if role == "admin":
        return
    with _lock:
        permissions, _error = _load()
    minimum = permissions[name]
    if _ROLE_LEVEL.get(role, -1) >= _ROLE_LEVEL[minimum]:
        return
    if minimum == "admin":
        raise AuthError("此操作仅限管理员", 403, "ADMIN_REQUIRED")
    raise AuthError("此工具仅限 Pro 或管理员使用", 403, "TOOL_ACCESS_DENIED")
