"""扩展数据创建与拉取的共享校验契约。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class FieldDef(BaseModel):
    name: str
    dtype: str = "string"        # string | int | float | bool
    label: str = ""


class CreateExtReq(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")
    label: str = Field(..., min_length=1, max_length=64)
    mode: Literal["snapshot", "timeseries"]
    fields: list[FieldDef] = Field(..., min_length=1)
    description: str = ""
    symbol_map: dict = {}   # {"type": "mapped", "col": "..."} 或 {"type": "computed", "from": "code", "method": "append_exchange"}
    code_map: dict = {}     # {"type": "mapped", "col": "..."} 或 {"type": "computed", "from": "symbol", "method": "strip_exchange"}
    market_level: bool = False  # 市场级表: 行=全市场每日一条, 无 symbol 列 (如择时状态序列)


class PullAuthReq(BaseModel):
    """拉取接口鉴权方式 (与自定义行情源 AuthConfig 同口径)。

    Key 本体存 secrets_store (secrets.json), 不写入 config.json。
    """
    type: Literal["none", "bearer", "header", "query"] = "none"
    header: str = Field("Authorization", min_length=1, max_length=64)  # bearer/header 用
    param: str = Field("token", min_length=1, max_length=64)          # query 用


class PullConfigReq(BaseModel):
    """定时拉取配置请求。"""
    url: str = Field(..., min_length=1)
    method: str = "GET"
    headers: dict[str, str] | None = None
    body: str | None = None
    response_path: str = ""          # dot-path to rows array
    field_map: dict[str, str] | None = None  # external → internal field name
    schedule_minutes: int = Field(1440, ge=1)
    enabled: bool = False
    schedule_time: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    schedule_period: Literal["daily", "weekly", "monthly"] = "daily"
    schedule_day: int = Field(1, ge=1, le=31)
    schedule_trading_only: bool = False
    time_window_start: str | None = None   # "HH:MM", None=不限
    time_window_end: str | None = None     # "HH:MM", None=不限
    # 接口按日查询的参数名 (如 "date"): 配置后支持历史回补, 且当日拉取也带日期参数
    date_param: str | None = Field(None, min_length=1, max_length=16, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    # 日期参数值的格式: iso/compact/ts_s/ts_ms (时间戳=该交易日北京时间 00:00:00), 缺省 iso
    date_format: str = "iso"
    # 日内序列表时间列名 (字段映射后的列名, 如 "ts"): 配置后同 symbol 允许多行,
    # 按 [symbol, time_field] 去重; 留空 = 每日快照表 (按 symbol 去重)
    time_field: str | None = Field(None, max_length=32)
    since_param: str | None = Field(None, min_length=1, max_length=16, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    granularity: Literal["auto", "intraday", "daily"] | None = None
    # 鉴权方式; 请求中缺省 (None) = 保留现有配置, {"type":"none"} = 关闭鉴权
    auth: PullAuthReq | None = None
    # 单次拉取请求超时 (秒), 默认 30 与历史行为一致; 大响应接口可调高
    timeout_seconds: int = Field(30, ge=5, le=300)
    page_param: str | None = Field(None, max_length=64)
    page_size_param: str | None = Field(None, max_length=64)
    page_size: int = Field(0, ge=0)
    page_start: int = Field(1, ge=0)
    max_pages: int = Field(20, ge=1, le=200)

    @model_validator(mode="after")
    def validate_schedule_and_cursor(self):
        if self.schedule_period == "weekly" and self.schedule_day > 7:
            raise ValueError("每周调度日期必须为 1（周一）至 7（周日）")
        if self.since_param and not (self.time_field or "").strip():
            raise ValueError("增量拉取需要配置时间字段")
        if self.since_param and self.since_param in {self.date_param, self.page_param, self.page_size_param}:
            raise ValueError("增量参数不能与日期或分页参数同名")
        return self


