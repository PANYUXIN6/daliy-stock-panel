"""AI 试运营规则的单一来源；运行配置与每个任务的规则快照分开保存。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FEATURES = (
    "assistant", "stock_analysis", "financial_analysis", "market_recap",
    "concept_rotation", "industry_rotation",
)
FLASH_MODELS = ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp")
Role = Literal["guest", "pro"]


class CreditPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # 点数是试运营的唯一用量门槛。旧持久化配置无此字段时也默认关闭金额限制。
    enforce_cost_limits: bool = False
    monthly_points: dict[str, int] = Field(default_factory=lambda: {"guest": 60, "pro": 4000})
    feature_points: dict[str, int] = Field(default_factory=lambda: {
        feature: 3 if feature == "assistant" else 5 for feature in FEATURES
    })
    # 货币一律使用整数微元（1 元 = 1,000,000 微元），不用浮点账。
    cost_per_point_micros: int = Field(default=20_000, gt=0, le=1_000_000)
    failure_budget_micros: dict[str, int] = Field(
        default_factory=lambda: {"guest": 300_000, "pro": 8_000_000},
    )
    model: str = "deepseek-flash"
    allowed_models: tuple[str, ...] = FLASH_MODELS
    # 官方高峰价，预留不依赖缓存命中或空闲折扣。
    input_micros_per_million: int = Field(default=2_000_000, gt=0)
    cached_input_micros_per_million: int = Field(default=40_000, ge=0)
    output_micros_per_million: int = Field(default=8_000_000, gt=0)
    assistant_max_output_tokens: int = Field(default=2048, ge=256, le=65_536)
    report_max_output_tokens: int = Field(default=6144, ge=256, le=65_536)
    assistant_max_calls: int = Field(default=8, ge=1, le=32)
    report_max_calls: int = Field(default=3, ge=1, le=8)
    min_output_tokens: int = Field(default=256, ge=1, le=2048)

    @model_validator(mode="after")
    def valid_rules(self):
        for values, keys, maximum in (
            (self.monthly_points, {"guest", "pro"}, 1_000_000),
            (self.failure_budget_micros, {"guest", "pro"}, 1_000_000_000),
            (self.feature_points, set(FEATURES), 100_000),
        ):
            if set(values) != keys or any(
                type(value) is not int or not 0 <= value <= maximum for value in values.values()
            ):
                raise ValueError("额度与入口规则必须完整，且为有效的非负整数")
        if any(value == 0 for value in self.feature_points.values()):
            raise ValueError("AI 入口扣点必须大于零")
        if self.monthly_points["pro"] < self.monthly_points["guest"]:
            raise ValueError("Pro 额度不能低于游客")
        if self.cached_input_micros_per_million > self.input_micros_per_million:
            raise ValueError("缓存输入价格不能超过未命中价格")
        if self.model != "deepseek-flash" or set(self.allowed_models) != set(FLASH_MODELS):
            raise ValueError("当前成本适配只支持官方 DeepSeek Flash 文本模型")
        if self.min_output_tokens > min(
            self.assistant_max_output_tokens, self.report_max_output_tokens,
        ):
            raise ValueError("最低输出预算不能超过任务输出上限")
        return self


def token_cost(tokens: int, micros_per_million: int) -> int:
    """向上取整，确保小额调用与预留不会因舍入低估费用。"""
    return (tokens * micros_per_million + 999_999) // 1_000_000


def cny(micros: int) -> str:
    return f"{micros // 1_000_000}.{micros % 1_000_000:06d}"
