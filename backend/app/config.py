"""全局配置 — 从环境变量 / .env 读取。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = Path(os.environ.get("TICKFLOW_ENV_FILE", str(_PROJECT_ROOT / ".env")))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # TickFlow
    tickflow_api_key: str = Field(default="", description="留空启用 free 模式")

    # AI
    ai_provider: str = "openai_compat"
    ai_base_url: str = "https://llm.runninghub.ai/v1"
    ai_api_key: str = ""
    ai_model: str = "openai/gpt-6-astra-saver"
    ai_codex_command: str = "codex"
    ai_codex_reasoning_effort: str = ""
    # 默认浏览器风格 UA,绕过 Cloudflare 等 CDN/WAF 的 Bot 拦截(Issue #8)。
    # 用户可在 AI 设置页按需修改。
    ai_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    # AI 输出上限 (max_tokens) 与输入上下文窗口上限 (约 token)。
    # 任务级 max_tokens 会被钳制到 ai_max_output_tokens; 输入估算超出上下文窗口时给出明确报错。
    # 钳制仅封顶不放大: 现有任务最多请求 4500, 上调默认值只影响用户自行调高任务上限的场景。
    # 上下文 128000 对齐当前主流模型底线 (GPT/Claude/GLM/DeepSeek/Kimi 均 ≥128k); 可在 AI 设置里调整。
    ai_max_output_tokens: int = 16384
    ai_context_window: int = 128000
    # AI 助手工具轮次检查点: 连续 N 轮工具调用未完成时弹「继续/停止」卡; 0=不检查。
    ai_round_checkpoint: int = 100

    # 两服务共享目录；只有 collector 拥有采集、配置写入和任务执行。
    service_role: Literal["web", "collector"] = "web"
    collector_url: str = "http://127.0.0.1:3019"
    ai_max_concurrent: int = Field(default=2, ge=1)

    # Server
    host: str = "0.0.0.0"
    port: int = 3018
    log_level: str = "INFO"
    backtest_range_guard: bool = False
    backtest_matrix_disk_cache_enabled: bool = True
    backtest_matrix_cache_max_mb: int = 512
    backtest_matrix_cache_prewarm: bool = True
    backtest_matrix_cache_prewarm_years: int = 5

    # polars collect 并发闸 — polars 共享执行器在多线程并发 collect 下存在死锁
    # (上游 #24448/#25754 同族), 限流并发是社区验证的缓解手段。background 限额
    # 保证预热/增量等后台计算不占满闸位饿死页面读请求。
    polars_collect_permits: int = 4
    polars_collect_background_permits: int = 2

    # 后端自愈看门狗 — 探测 collect 闸与全局写锁, 连续失败即退出交由
    # supervisor 拉起 (见 app/watchdog.py)。误伤防护靠保守阈值。
    watchdog_enabled: bool = True
    watchdog_interval_s: float = 30.0
    watchdog_probe_timeout_s: float = 15.0
    watchdog_failure_threshold: int = 2

    # 策略批量执行 (run_all / 策略页全量跑) 的并发 worker 上限。实测 2026-09-07:
    # polars eager 操作内部已多线程并行, 外层再并发 4 worker 属超订, 41 策略
    # 299.6s 慢于串行 — 默认 1 (串行)。保留开关供配合 POLARS_MAX_THREADS 调优实验。
    strategy_run_all_workers: int = 1

    # run_all 渐进式返回: HTTP 同步等待时限 (秒)。策略按历史耗时升序执行,
    # 到点后已算完的随响应返回, 未算完的转后台继续算并逐个写入策略缓存,
    # 前端轮询 cached-summary 点亮卡片。0 = 关闭 (整段阻塞, 旧行为)。
    strategy_run_all_first_return_s: float = 15.0

    # Optional complete override for the built-in administrator on an empty database.
    # Legacy AUTH_PASSWORD has no authority.
    auth_admin_username: str = ""
    auth_admin_password: str = ""
    auth_cookie_secure: bool = False  # automatically enabled for HTTPS requests
    auth_trusted_proxies: str = ""  # explicit comma-separated IP/CIDR list
    auth_allowed_origins: str = ""  # extra browser origins, e.g. local Vite dev server

    # 服务器与本地源码默认使用项目根 data/，可通过 DATA_DIR 覆盖。
    data_dir: Path = _PROJECT_ROOT / "data"

    tiers_yaml: Path = _PROJECT_ROOT / "tiers.yaml"
    static_dir: Path = _PROJECT_ROOT / "frontend" / "dist"

    @model_validator(mode="after")
    def _resolve_paths(self) -> Settings:
        """确保 data_dir 是绝对路径（环境变量传入的相对路径基于项目根目录解析）。"""
        if not self.data_dir.is_absolute():
            # 相对路径基于项目根目录解析，而非 CWD
            self.data_dir = (_PROJECT_ROOT / self.data_dir).resolve()
        if self.backtest_matrix_cache_max_mb <= 0:
            raise ValueError("backtest_matrix_cache_max_mb must be positive")
        if self.backtest_matrix_cache_prewarm_years <= 0:
            raise ValueError("backtest_matrix_cache_prewarm_years must be positive")
        if self.ai_max_output_tokens <= 0:
            raise ValueError("ai_max_output_tokens must be positive")
        if self.ai_context_window <= 0:
            raise ValueError("ai_context_window must be positive")
        if self.polars_collect_permits < 2:
            raise ValueError("polars_collect_permits must be >= 2")
        if not 1 <= self.polars_collect_background_permits < self.polars_collect_permits:
            raise ValueError(
                "polars_collect_background_permits must be in [1, polars_collect_permits)"
            )
        if self.watchdog_interval_s <= 0 or self.watchdog_probe_timeout_s <= 0:
            raise ValueError("watchdog intervals must be positive")
        if self.watchdog_failure_threshold < 1:
            raise ValueError("watchdog_failure_threshold must be >= 1")
        if self.strategy_run_all_workers < 1:
            raise ValueError("strategy_run_all_workers must be >= 1")
        if self.strategy_run_all_first_return_s < 0:
            raise ValueError("strategy_run_all_first_return_s must be >= 0")
        return self

    @property
    def use_free_mode(self) -> bool:
        """是否走 Free 模式。优先看 secrets.json,其次看 .env。"""
        from app import secrets_store
        return not secrets_store.get_tickflow_key()


settings = Settings()
