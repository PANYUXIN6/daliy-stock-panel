"""内置概念/行业：fuyao 批量反查当前股票分类，保留扩展表字段契约。

启动只建配置和迁移退役的出厂 URL，不联网、不改用户开关和自定义源。
同步按本地股票维表查询；每张表完整取数后原子替换快照，缺组/失败不写入。
行业为接口原始名称，不拼造已不提供的父子层级；旧层级表仍可正常读取。
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import polars as pl

from app.market_time import cn_today
from app.plugins.fuyao.client import BASE_URL, CLASSIFICATIONS_PATH
from app.services.ext_data import (
    ExtConfig,
    ExtConfigStore,
    ExtField,
    PullConfig,
    rows_to_parquet,
)

logger = logging.getLogger(__name__)

_CONCEPT_DATA_URL = f"{BASE_URL}{CLASSIFICATIONS_PATH}?type=cn_concept"
_INDUSTRY_DATA_URL = f"{BASE_URL}{CLASSIFICATIONS_PATH}?type=industry"
_RETIRED_URLS = {
    "ext_gn_ths": "https://shy313.com/api/plugins/market_flow/exports/ths-concepts",
    "ext_hy_ths": "https://shy313.com/api/plugins/market_flow/exports/ths-industries",
}


# ---------------------------------------------------------------------------
# 预设定义: 字段结构 + 拉取配方
# ---------------------------------------------------------------------------

def _concept_preset() -> ExtConfig:
    """扩展概念 (ext_gn_ths)。

    接口结构: [{symbol, name, concepts: [概念1, 概念2, ...]}]
    本地 schema: 股票代码 / 股票简称 / 所属概念(分号拼接) / symbol / code
    """
    return ExtConfig(
        id="ext_gn_ths",
        label="扩展概念",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("code", "string", "代码"),
            ExtField("股票代码", "string", "股票代码"),
            ExtField("股票简称", "string", "股票简称"),
            ExtField("所属概念", "string", "所属概念"),
        ],
        description="同花顺概念分类 (fuyao 批量反查，复用数据源设置中的 API Key)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=_CONCEPT_DATA_URL,
            method="GET",
            schedule_minutes=1440,
            # enabled=False: ensure_builtin_presets 承诺启动不拉取, PullScheduler
            # 只调度 enabled 配置; 手动获取走 fetch_preset 独立路径不受影响 (#199)
            enabled=False,
        ),
    )


def _industry_preset() -> ExtConfig:
    """扩展行业 (ext_hy_ths)。

    fuyao 提供独立行业名称；多个行业以分号分隔，不解释为父子层级。
    本地 schema: 股票代码 / 股票简称 / 所属同花顺行业 / symbol / code
    """
    return ExtConfig(
        id="ext_hy_ths",
        label="扩展行业",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("code", "string", "代码"),
            ExtField("股票代码", "string", "股票代码"),
            ExtField("股票简称", "string", "股票简称"),
            ExtField("所属同花顺行业", "string", "所属同花顺行业"),
        ],
        description="同花顺行业分类 (fuyao 原始行业名称，无父子层级；复用数据源 API Key)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=_INDUSTRY_DATA_URL,
            method="GET",
            schedule_minutes=1440,
            # 同概念 preset: 出厂禁用, 避免启动即网络拉取 (#199)
            enabled=False,
        ),
    )


def _presets() -> list[ExtConfig]:
    return [_concept_preset(), _industry_preset()]


# ---------------------------------------------------------------------------
# 接口结构 → 本地 schema 转换 (仅预设使用)
# ---------------------------------------------------------------------------

def _symbol_to_code(symbol: str) -> str:
    """symbol (000001.SZ) → code (000001)。"""
    return symbol.split(".", 1)[0] if "." in symbol else symbol


def _dimension_label(value: object) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"nan", "none", "null"} else text


def _flatten_concept_rows(raw_rows: list[dict]) -> list[dict]:
    """概念: concepts 数组 → 分号拼接成「所属概念」字符串。

    [{symbol, name, concepts:[...]}] → [{股票代码, 股票简称, 所属概念, symbol, code}]
    注: code 由 symbol 派生 (000001.SZ → 000001), 因 rows_to_parquet 不执行 code_map。
    """
    out: list[dict] = []
    for r in raw_rows:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        concepts = r.get("concepts") or []
        labels = [label for c in concepts if (label := _dimension_label(c))]
        out.append({
            "股票代码": sym,
            "股票简称": r.get("name") or "",
            "所属概念": ";".join(labels),
            "symbol": sym,
            "code": _symbol_to_code(sym),
        })
    return out


def _flatten_industry_rows(raw_rows: list[dict], *, hierarchical: bool = True) -> list[dict]:
    """旧层级按横杠拼接；平行行业名称按分号拼接，不伪造层级。

    [{symbol, name, industries:[...]}] → [{股票代码, 股票简称, 所属同花顺行业, symbol, code}]
    """
    out: list[dict] = []
    for r in raw_rows:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        inds = r.get("industries") or []
        labels = [label for i in inds if (label := _dimension_label(i))]
        out.append({
            "股票代码": sym,
            "股票简称": r.get("name") or "",
            "所属同花顺行业": ("-" if hierarchical else ";").join(labels),
            "symbol": sym,
            "code": _symbol_to_code(sym),
        })
    return out


# ---------------------------------------------------------------------------
# 拉取执行：供应商认证、限频和协议转换由 fuyao provider 负责
# ---------------------------------------------------------------------------

def _classification_instruments(data_dir: Path) -> pl.DataFrame:
    path = data_dir / "instruments" / "instruments.parquet"
    if not path.exists():
        raise ValueError("请先同步股票维表，再获取板块分类")
    frame = pl.read_parquet(path)
    for column, value in (("type", "stock"), ("security_type", "a_share"), ("is_current", True)):
        if column in frame.columns:
            frame = frame.filter(pl.col(column).is_null() | (pl.col(column) == value))
    if "listing_date" in frame.columns:
        listed = pl.col("listing_date").cast(pl.String).str.slice(0, 10).str.to_date(strict=False)
        frame = frame.filter(listed.is_null() | (listed <= cn_today()))
    if frame.is_empty() or "symbol" not in frame.columns:
        raise ValueError("股票维表为空，无法确认分类查询范围")
    if frame["symbol"].null_count() or frame["symbol"].n_unique() != len(frame):
        raise ValueError("股票维表包含缺失或重复证券，请先修复维表")
    return frame


async def _fetch_classification_rows(
    config_id: str, data_dir: Path, on_progress: Callable[[int, int], None] | None = None,
) -> list[dict]:
    from app.plugins.fuyao.provider import FuyaoProvider

    stopped = Event()

    def fetch() -> list[dict]:
        instruments = _classification_instruments(data_dir)
        names = dict(zip(instruments["symbol"], instruments["name"], strict=True)) if "name" in instruments.columns else {}
        provider = FuyaoProvider()
        try:
            rows = provider.get_classifications(
                instruments["symbol"].to_list(), "concept" if config_id == "ext_gn_ths" else "industry",
                on_progress=on_progress,
                cancel_event=stopped,
            )
            return [{**row, "name": names.get(row["symbol"]) or ""} for row in rows]
        finally:
            provider.close()

    task = asyncio.create_task(asyncio.to_thread(fetch))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stopped.set()
        # 调度器取消时也等待在途请求结束，不能把后台线程留到下一轮。
        with suppress(Exception):
            await task
        raise


@dataclass(frozen=True)
class PreparedPreset:
    config: ExtConfig
    rows: list[dict]
    observation: dict | None = None


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def get_preset(config_id: str) -> ExtConfig | None:
    """按 id 取预设定义 (供 API 层校验 id 合法性)。"""
    for c in _presets():
        if c.id == config_id:
            return c
    return None


def is_builtin_classification_pull(config: ExtConfig) -> bool:
    """只接管内置快照的已知出厂配方，用户自定义 URL 仍走通用拉取。"""
    preset = get_preset(config.id)
    return bool(preset and config.mode == "snapshot" and config.pull
                and config.pull.method.upper() == "GET"
                and config.pull.url in {preset.pull.url, _RETIRED_URLS[config.id]})


async def ensure_builtin_presets(data_dir: Path) -> None:
    """启动时: 为缺失的预设创建 config.json (含 pull 配置), 但【不拉取数据】。

    设计: 数据获取改为用户在数据页手动点「获取数据」触发, 避免启动时
    网络请求阻塞, 也避免「自动拉取」与「用户自主控制」的预期冲突。

    安全保证:
      - 已存在只迁移退役的出厂 URL，保留自定义地址、字段及调度开关
      - 只写 config.json, 失败只记 warning 不阻断启动
    """
    store = ExtConfigStore(data_dir)

    for config in _presets():
        existing = store.get(config.id)
        try:
            if existing is not None:
                if is_builtin_classification_pull(existing) and existing.pull.url == _RETIRED_URLS[config.id]:
                    existing.pull.url = config.pull.url
                    store.upsert(existing)
                continue
            store.upsert(config)
            logger.info("内置扩展表 %s 配置已就绪 (待用户手动获取数据)", config.id)
        except Exception as e:  # noqa: BLE001 — 内置扩展配置的单项准备失败不阻断其他预设及应用启动
            logger.warning("内置扩展表 %s 配置写入失败 (不影响启动): %s", config.id, e, exc_info=True)


async def fetch_preset(config_id: str, data_dir: Path) -> int:
    """手动触发某个预设的数据拉取 (供 API 调用)。

    Raises:
        ValueError: config_id 不是内置预设
        Exception: 网络请求/解析/写入失败 (由 API 层转 HTTP 错误)
    """
    return commit_prepared_preset(await prepare_preset(config_id, data_dir), data_dir)


async def prepare_preset(
    config_id: str, data_dir: Path, *, on_progress: Callable[[int, int], None] | None = None,
) -> PreparedPreset:
    """只请求和转换单表，不修改共享配置或文件，供管道的网络预取使用。"""
    config = get_preset(config_id)
    if config is None:
        raise ValueError(f"未知的内置预设: {config_id}")

    raw = await _fetch_classification_rows(config_id, data_dir, on_progress)
    rows = (_flatten_concept_rows(raw) if config_id == "ext_gn_ths"
            else _flatten_industry_rows(raw, hierarchical=False))
    if not rows:
        raise ValueError(f"接口返回 0 行: {config.pull.url}")
    from uuid import uuid4

    from app.market_time import cn_now

    symbols = [row.get("symbol") for row in raw]
    if any(not isinstance(s, str) or not s.strip() for s in symbols) or len(set(symbols)) != len(symbols):
        raise ValueError("分类完整响应包含缺失或重复证券标识")
    memberships = []
    for row in raw:
        labels = row.get("concepts" if config_id == "ext_gn_ths" else "industries")
        if not isinstance(labels, list):
            raise ValueError("分类完整响应缺少成员列表")
        labels = [_dimension_label(value) for value in labels]
        if any(not value for value in labels):
            raise ValueError("分类完整响应存在无效标签")
        for label in sorted(set(labels)):
            memberships.append({"symbol": row["symbol"], "sector_id": label, "sector_name": label,
                                "source": "concept" if config_id == "ext_gn_ths" else "industry"})
    observation = {"id": uuid4().hex, "source": config.pull.url,
                   "obtained_at": cn_now().isoformat(), "source_effective_date": None,
                   "source_published_at": None, "source_version": None,
                   "complete": True, "completeness_basis": "本地当前股票维表逐批反查，每只证券均有唯一响应分组及有效分类列表",
                   "membership_semantics": "fuyao-flat-v1",
                   "limitation": "当前关系快照，无历史生效日；行业保留接口原始名称，不提供父子层级",
                   "symbols": symbols, "memberships": memberships}
    return PreparedPreset(config, rows, observation)


def commit_prepared_preset(prepared: PreparedPreset, data_dir: Path, *, keep_strategy_cache: bool = False) -> int:
    """由所属任务线程提交单表；保留用户已保存的配置。"""
    config = prepared.config
    store = ExtConfigStore(data_dir)
    if store.get(config.id) is None:
        store.upsert(config)
    n = rows_to_parquet(prepared.rows, config, data_dir, snapshot_date=cn_today(),
                        replace_snapshot=True, keep_strategy_cache=keep_strategy_cache)
    logger.info("内置扩展表 %s 拉取并提交成功: %d 行", config.id, n)
    return n


async def sync_classifications(
    data_dir: Path,
    results: dict[str, int],
    on_progress: Callable[[str, int, str], None],
) -> dict[str, int]:
    """两路纯取数并发，主线程顺序写入，保留部分结果和取消检查点。"""
    presets = _presets()
    on_progress("classifications", 0, "并行获取概念与行业分类…")
    prepared = await asyncio.gather(
        *(prepare_preset(config.id, data_dir, on_progress=lambda done, total, label=config.label:
                         on_progress("classifications", int(80 * done / total), f"{label}：{done}/{total} 只"))
          for config in presets), return_exceptions=True,
    )
    for index, config in enumerate(presets):
        on_progress("classifications", int(index * 95 / len(presets)), f"同步{config.label}…")
        outcome = prepared[index]
        if isinstance(outcome, BaseException):
            raise outcome
        results[config.id] = commit_prepared_preset(outcome, data_dir)
        # 先记录已写入数据, 再让进度回调检查取消, 保证取消也会发布本表。
        on_progress("classifications", int((index + 1) * 95 / len(presets)), f"{config.label}同步完成, {results[config.id]} 行")
    on_progress("done", 100, "概念与行业分类同步完成")
    return results
