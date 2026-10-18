"""财务数据独立同步服务。

解耦于 K-line 管道, 自有调度 + 自有存储。
能力门控: Cap.FINANCIAL (Expert 套餐)
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from app.data_providers.base import ProviderFinancialDataUnavailableError, ProviderRateLimitError
from app.services.financial_versions import read_versions, record_versions
from app.services.fs_utils import atomic_write_parquet, atomic_write_text
from app.tickflow.capabilities import Cap, CapabilitySet

logger = logging.getLogger(__name__)

# 每个 API 请求最多 100 个标的
_BATCH_SIZE = 100

# 财务报表 + 历史股本表
FINANCIAL_TABLES = ("balance_sheet", "cash_flow", "metrics", "income", "shares")
# 当前采集范围；其余表保留只读协议，不能被旧的全表或单表入口重新采集。
FINANCIAL_SYNC_TABLES = ("income",)
_RATE_LIMIT_COOLDOWN_S = 600


# ================================================================
# 同步函数
# ================================================================

def _get_symbols(data_dir: Path) -> list[str]:
    """读取本地股票池，并按供应商可选清单核对全市场财务请求范围。"""
    inst_path = data_dir / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return []
    try:
        df = pl.read_parquet(inst_path, columns=["symbol"])
        symbols = df["symbol"].to_list()
    except (OSError, pl.exceptions.PolarsError) as e:
        logger.warning("读取 instruments 失败: %s", e, exc_info=True)
        return []
    if not symbols or not _financial_is_custom():
        return symbols
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    provider_name = preferences.get_financial_provider()
    provider = custom_sources.get_provider(provider_name)
    universe_method = getattr(provider, "get_financial_universe", None)
    if not callable(universe_method):
        return symbols
    # 供应商负责代码格式与覆盖范围；不能从名称、代码前缀推测退市状态。
    universe = set(universe_method())
    if not universe:
        raise RuntimeError("财务数据源返回空股票范围，停止同步")
    provider_only = sorted(universe - set(symbols))
    # 全市场同步不能漏掉供应商已有、基础维表尚未收录的新代码。
    selected = list(dict.fromkeys(symbol for symbol in symbols if symbol in universe)) + provider_only
    report = {
        "provider": provider_name,
        "checked_at": datetime.now(UTC).isoformat(),
        "local_symbols": len(symbols),
        "provider_symbols": len(universe),
        "requested_symbols": len(selected),
        "excluded_symbols": sorted(set(symbols) - universe),
        "provider_only_symbols": provider_only,
    }
    out = data_dir / "financials"
    out.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out / "universe.json", json.dumps(report, ensure_ascii=False, indent=2))
    logger.info("财务股票池 %s: 本地 %d，供应商 %d，请求 %d，源未覆盖 %d，维表缺少 %d",
                provider_name, len(symbols), len(universe), len(selected),
                len(report["excluded_symbols"]), len(report["provider_only_symbols"]))
    return selected


def _financial_is_custom() -> bool:
    """当前财务数据源是否走 custom (用于绕过 TickFlow Expert 套餐门槛)。"""
    from app.services import preferences
    provider = preferences.get_financial_provider()
    if provider == "tickflow":
        return False
    from app.data_providers import custom as custom_sources
    return custom_sources.provider_has_dataset(provider, "financial")


def _fetch_table(
    table: str,
    symbols: list[str],
    capset: CapabilitySet,
    latest_only: bool = True,
    on_progress=None,
    *, cancel_event: threading.Event | None = None,
) -> pl.DataFrame:
    """通过当前财务数据源拉取一张标准化财务表。"""
    if on_progress:
        on_progress(0, 1)
    is_custom = _financial_is_custom()
    if not is_custom and not capset.has(Cap.FINANCIAL):
        logger.info("sync_%s skipped: no FINANCIAL capability", table)
        return pl.DataFrame()
    if not symbols:
        logger.warning("sync_%s skipped: no symbols", table)
        return pl.DataFrame()

    # 自定义数据源分流
    if is_custom:
        from app.data_providers import custom as custom_sources
        from app.services import preferences

        try:
            source = preferences.get_financial_provider()
            provider = custom_sources.get_provider(source)
            parameters = inspect.signature(provider.get_financials).parameters
            cancel_kwargs = ({"cancel_event": cancel_event}
                             if cancel_event is not None and "cancel_event" in parameters else {})
            if "on_chunk_done" in parameters:
                # 支持原生进度的源自行控频；旧插件仍按原有 100 股批次调用。
                df = provider.get_financials(table, symbols, latest_only=latest_only,
                                            on_chunk_done=on_progress, **cancel_kwargs)
            else:
                frames: list[pl.DataFrame] = []
                total_batches = (len(symbols) + _BATCH_SIZE - 1) // _BATCH_SIZE
                for i in range(0, len(symbols), _BATCH_SIZE):
                    batch_num = i // _BATCH_SIZE + 1
                    if on_progress:
                        on_progress(batch_num - 1, total_batches)
                    frame = provider.get_financials(table, symbols[i:i + _BATCH_SIZE],
                                                   latest_only=latest_only, **cancel_kwargs)
                    if not frame.is_empty() and "symbol" in frame.columns:
                        frames.append(frame)
                    if on_progress:
                        on_progress(batch_num, total_batches)
                df = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        except ProviderFinancialDataUnavailableError as exc:
            if not exc.data.is_empty():
                exc.data = exc.data.with_columns(pl.lit(source).alias("source"))
            raise
        except ProviderRateLimitError:
            raise
        except Exception as e:
            logger.warning("sync_%s custom provider failed: %s", table, e)
            raise
        if df.is_empty() or "symbol" not in df.columns:
            return pl.DataFrame()
        return df.with_columns(pl.lit(source).alias("source"))

    from app.tickflow.client import get_client
    tf = get_client()

    # 分批拉取
    api_method = {
        "metrics": tf.financials.metrics,
        "income": tf.financials.income,
        "balance_sheet": tf.financials.balance_sheet,
        "cash_flow": tf.financials.cash_flow,
        "shares": getattr(tf.financials, "shares", None),
    }[table]
    if api_method is None:
        logger.warning("sync_shares skipped: current TickFlow SDK does not support shares")
        return pl.DataFrame()

    all_records: list[dict] = []
    errors: list[Exception] = []
    total_batches = (len(symbols) + _BATCH_SIZE - 1) // _BATCH_SIZE

    for i in range(0, len(symbols), _BATCH_SIZE):
        chunk = symbols[i : i + _BATCH_SIZE]
        batch_num = i // _BATCH_SIZE + 1
        if on_progress:
            on_progress(batch_num - 1, total_batches)
        try:
            data = api_method(chunk, latest=latest_only)
            # data 格式: { "600519.SH": [record, ...], ... }
            if isinstance(data, dict):
                for sym, records in data.items():
                    if isinstance(records, list):
                        for rec in records:
                            if isinstance(rec, dict):
                                rec["symbol"] = sym
                                all_records.append(rec)
            logger.debug("sync_%s batch %d/%d: %d records", table, batch_num, total_batches, len(data) if isinstance(data, dict) else 0)
        except Exception as e:  # noqa: BLE001 — SDK 单批错误汇总给后续失败判定，保留其他批次的成功结果
            logger.warning("sync_%s batch %d/%d failed: %s", table, batch_num, total_batches, e, exc_info=True)
            errors.append(e)

    if on_progress:
        on_progress(total_batches, total_batches)
    if not all_records:
        if errors:
            raise errors[0]
        return pl.DataFrame()

    df = pl.DataFrame(all_records)
    if df.is_empty() or "symbol" not in df.columns:
        return pl.DataFrame()
    return df.with_columns(pl.lit("tickflow").alias("source"))


def _write_table(table: str, df: pl.DataFrame, data_dir: Path) -> int:
    if df.is_empty() or "symbol" not in df.columns:
        return 0

    # 写入 Parquet (全量覆盖)
    out_dir = data_dir / "financials" / table
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "part.parquet"
    atomic_write_parquet(df, out_file)
    from app.services.shared_runtime import publish_data_update

    publish_data_update(data_dir, ["financials"])

    logger.info("sync_%s done: %d records written", table, len(df))
    return len(df)


def _sync_table(
    table: str,
    symbols: list[str],
    data_dir: Path,
    capset: CapabilitySet,
    latest_only: bool = True,
    on_progress=None,
    *, cancel_event: threading.Event | None = None,
) -> int:
    """同步单张财务表。返回写入的行数。"""
    fetched = _fetch_table(table, symbols, capset, latest_only=latest_only,
                           **({"on_progress": on_progress} if on_progress else {}),
                           **({"cancel_event": cancel_event} if cancel_event is not None else {}))
    captured = _record_table(table, fetched, data_dir)
    return _write_table(table, captured, data_dir)


def _record_table(table: str, frame: pl.DataFrame, data_dir: Path) -> pl.DataFrame:
    from app.services import preferences

    if frame.is_empty():
        return frame
    # 取数后路由配置可能已变化，版本归属以实际执行请求的源为准。
    sources = frame["source"].drop_nulls().unique().to_list() if "source" in frame.columns else []
    if len(sources) > 1:
        raise ValueError("同一财务采集不能混合多个数据源")
    source = sources[0] if sources else preferences.get_financial_provider()
    return record_versions(data_dir, table, frame, source, datetime.now(UTC))


def _merge_report_history(*frames: pl.DataFrame) -> pl.DataFrame:
    """浏览表每期取完整最新记录，不再跨版本或数据源逐列拼接。"""
    valid = [
        frame
        for frame in frames
        if not frame.is_empty() and {"symbol", "period_end"} <= set(frame.columns)
    ]
    if not valid:
        return pl.DataFrame()
    merged = (
        pl.concat(valid, how="diagonal_relaxed")
        .filter(pl.col("symbol").is_not_null() & pl.col("period_end").is_not_null())
    )
    keys = ["symbol", "period_end"]
    if "disclosure_type" in merged.columns:
        # 历史浏览表未标注类型，但该五表契约属于正式财务数据。
        merged = merged.with_columns(pl.col("disclosure_type").fill_null("formal"))
        keys.append("disclosure_type")
    sort_keys = keys + (
        ["announce_date"] if "announce_date" in merged.columns else []
    )
    if "observed_at" in merged.columns:
        sort_keys = [*keys, "observed_at"]
    merged = merged.sort(sort_keys, nulls_last=False)
    return merged.unique(subset=keys, keep="last", maintain_order=True).sort(keys)


def _financial_batch_policy() -> tuple[str, int, int] | None:
    """仅声明可分批保存的源启用恢复，旧插件保持整表提交契约。"""
    if not _financial_is_custom():
        return None
    from app.data_providers import custom as sources
    from app.services import preferences

    source = preferences.get_financial_provider()
    provider = sources.get_provider(source)
    size = getattr(provider, "financial_sync_batch_size", 0)
    periods = getattr(provider, "financial_history_periods", 0)
    return (source, int(size), int(periods)) if size > 0 else None


def unfinished_financial_tables(data_dir: Path) -> list[str]:
    """恢复同源同窗口的未完成财报与指标，完整结束的表不重新观察。"""
    policy = _financial_batch_policy()
    if policy is None:
        raise ValueError("当前财务数据源不支持断点续采，请使用全部同步")
    source, _size, periods = policy
    tables = []
    for table in FINANCIAL_SYNC_TABLES:
        path = data_dir / "financials" / "sync_progress" / f"{table}.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        if (state.get("status") != "completed" or state.get("source") != source
                or state.get("history_periods") != periods):
            tables.append(table)
    return tables


def wait_financial_rate_limit(attempt: int, on_wait, cancel_event=None) -> bool:
    """有恢复能力的后台任务冷却后续跑；客户端本身不重试限流响应。"""
    if _financial_batch_policy() is None:
        return False
    from app.services.pipeline_jobs import JobCancelledError

    seconds = min(_RATE_LIMIT_COOLDOWN_S * 2 ** min(attempt - 1, 2), 1800)
    deadline = time.monotonic() + seconds
    while True:
        remaining = max(0, int(deadline - time.monotonic()))
        on_wait(remaining)
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelledError("financial")
        if remaining == 0:
            return True
        if cancel_event is not None:
            cancel_event.wait(min(30, remaining))
        else:
            time.sleep(min(30, remaining))


def sync_candidate_income(
    symbols: list[str], data_dir: Path, capset: CapabilitySet, on_progress=None,
    *, cancel_event: threading.Event | None = None,
    before_write: Callable[[], None] | None = None,
) -> dict:
    """在采集任务唯一写入槽内更新候选股，不改全市场断点或同步完成时间。

    逐股保存完整利润表窗口；单股取数失败仍继续其他候选。版本时间由既有
    record_versions 确定，不能把本次才观察到的记录提前至缺口形成日。
    before_write 在版本与浏览表分别写入前复核延迟操作的授权。
    """
    from app.services import preferences
    from app.services.pipeline_jobs import JobCancelledError

    if preferences.get_financial_provider() != "fuyao" or not _financial_is_custom():
        raise ValueError("候选股利润核验需要启用可用的扶摇财务数据源")
    symbols = list(dict.fromkeys(symbol.strip() for symbol in symbols if symbol.strip()))
    total = len(symbols)
    result = {"requested": total, "updated_symbols": [], "errors": {}}
    if not symbols:
        return result

    def progress(done):
        if on_progress:
            on_progress(done, total)
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelledError("financial_candidates")

    progress(0)
    # 局部更新不能把损坏的全市场文件当空表再覆盖；开始请求前严格核实两份文件。
    path = data_dir / "financials" / "income" / "part.parquet"
    existing = pl.read_parquet(path) if path.exists() else pl.DataFrame()
    read_versions(data_dir, "income")
    for index, symbol in enumerate(symbols):
        attempts = 0
        while True:
            progress(index)
            if preferences.get_financial_provider() != "fuyao" or not _financial_is_custom():
                raise ValueError("财务数据源已改变，停止候选股利润核验")
            try:
                fetched = _fetch_table(
                    "income", [symbol], capset, latest_only=False,
                    on_progress=lambda *_, done=index: progress(done), cancel_event=cancel_event,
                )
            except ProviderRateLimitError:
                attempts += 1
                if wait_financial_rate_limit(attempts, lambda _, done=index: progress(done), cancel_event):
                    continue
                result["errors"][symbol] = "利润表接口限流，本次未完成核验"
                break
            except JobCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 单股取数失败留痕，不阻断其他候选
                progress(index)
                result["errors"][symbol] = f"利润表获取失败：{exc}"
                break

            progress(index)
            if fetched.is_empty():
                result["errors"][symbol] = "未获取到可用利润表数据"
            elif not {"symbol", "period_end"} <= set(fetched.columns):
                result["errors"][symbol] = "利润表缺少股票代码或报告期"
            elif set(fetched["symbol"].to_list()) - {symbol}:
                result["errors"][symbol] = "利润表返回了请求范围以外的股票，已拒绝保存"
            elif "source" not in fetched.columns or set(fetched["source"].to_list()) != {"fuyao"}:
                result["errors"][symbol] = "利润表数据源与扶摇核验范围不一致，已拒绝保存"
            else:
                if before_write:
                    before_write()
                try:
                    captured = _record_table("income", fetched, data_dir)
                except ValueError as exc:
                    result["errors"][symbol] = f"利润表记录无效：{exc}"
                else:
                    existing = _merge_report_history(existing, captured)
                    if before_write:
                        before_write()
                    _write_table("income", existing, data_dir)
                    result["updated_symbols"].append(symbol)
            break
        progress(index + 1)
    return result


def _sync_financial_batches(table, symbols, data_dir, capset, existing, policy,
                            on_progress=None, cancel_event=None) -> int:
    """完整批次先保存版本和浏览表，再记录进度；失败批次不标记完成。"""
    from app.services import preferences
    from app.services.pipeline_jobs import JobCancelledError

    source, size, periods = policy
    path = data_dir / "financials" / "sync_progress" / f"{table}.json"
    checkpoint = json.loads(path.read_text()) if path.exists() else {}
    if (checkpoint.get("status") != "running" or checkpoint.get("source") != source
            or checkpoint.get("history_periods") != periods):
        checkpoint = {"source": source, "table": table, "history_periods": periods,
                      "symbols": symbols, "completed_symbols": [], "status": "running",
                      "started_at": datetime.now(UTC).isoformat(), "rows_captured": 0}
    # 股票池重排、新增或移除不改变已采集代码的版本事实；只补当前范围的未完成代码。
    checkpoint["symbols"] = symbols
    completed = set(checkpoint["completed_symbols"]) & set(symbols)
    # 本轮经人工确认的例外单独留痕，不能伪装成取数成功或成为永久黑名单。
    skipped = {symbol: reason for symbol, reason in checkpoint.get("skipped_symbols", {}).items()
               if symbol in symbols and symbol not in completed}
    checkpoint["skipped_symbols"] = skipped
    pending = [symbol for symbol in symbols if symbol not in completed and symbol not in skipped]
    if skipped:
        logger.warning("财务 %s 本轮已有跳过记录 %d 只: %s", table, len(skipped), skipped)
    path.parent.mkdir(parents=True, exist_ok=True)

    def save_progress():
        checkpoint["completed_symbols"] = sorted(completed)
        checkpoint["updated_at"] = datetime.now(UTC).isoformat()
        atomic_write_text(path, json.dumps(checkpoint, ensure_ascii=False))

    def progress(done=0, total=1, *, base=0, count=0):
        if on_progress:
            on_progress(len(skipped) + base + int(count * done / max(total, 1)), len(symbols))
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelledError("financial")

    save_progress()
    progress(base=len(completed))
    for index in range(0, len(pending), size):
        if preferences.get_financial_provider() != source:
            raise RuntimeError("财务数据源已改变，停止当前恢复任务")
        chunk = pending[index:index + size]
        count = len(chunk)
        base = len(completed)
        missing_symbols = {}
        try:
            fetched = _fetch_table(
                table, chunk, capset, latest_only=False,
                on_progress=lambda done, total, base=base, count=count: progress(done, total, base=base, count=count),
                **({"cancel_event": cancel_event} if cancel_event is not None else {}),
            )
        except ProviderFinancialDataUnavailableError as exc:
            if table != "metrics":
                raise
            missing_symbols = exc.missing_symbols
            fetched = exc.data
            if (not missing_symbols or not set(missing_symbols) <= set(chunk)
                    or (not fetched.is_empty() and set(fetched["symbol"]) & set(missing_symbols))):
                raise ValueError("财务缺数记录与本批次成功数据不一致") from exc
        if not fetched.is_empty():
            captured = _record_table(table, fetched, data_dir)
            existing = _merge_report_history(existing, captured)
            _write_table(table, existing, data_dir)
            checkpoint["rows_captured"] += captured.height
        skipped.update(missing_symbols)
        completed.update(symbol for symbol in chunk if symbol not in missing_symbols)
        if missing_symbols:
            logger.warning("财务 %s 本轮确认缺数跳过: %s", table, missing_symbols)
        save_progress()
        progress(base=len(completed))
    checkpoint["status"] = "completed"
    save_progress()
    return existing.height if checkpoint["rows_captured"] else 0


def _sync_history_table_for_symbols(
    table: str,
    symbols: list[str],
    data_dir: Path,
    capset: CapabilitySet,
    on_progress=None,
    *, cancel_event: threading.Event | None = None,
) -> int:
    """财报每轮重新观察源可返回的历史窗口，以捕获旧期更新；股本沿用增量。"""
    existing = get_financial_df(data_dir, table)
    policy = _financial_batch_policy() if table != "shares" else None
    if policy is not None:
        return _sync_financial_batches(table, symbols, data_dir, capset, existing, policy,
                                      on_progress, cancel_event)
    fetch_kwargs = {"on_progress": on_progress} if on_progress else {}
    if cancel_event is not None:
        fetch_kwargs["cancel_event"] = cancel_event
    if table != "shares":
        fetched = _fetch_table(table, symbols, capset, latest_only=False, **fetch_kwargs)
        if fetched.is_empty():
            return 0
        captured = _record_table(table, fetched, data_dir)
        return _write_table(table, _merge_report_history(existing, captured), data_dir)
    if existing.is_empty() or not {"symbol", "period_end"} <= set(existing.columns):
        return _sync_table(table, symbols, data_dir, capset, latest_only=False, **fetch_kwargs)

    existing_symbols = set(existing["symbol"].drop_nulls().to_list())
    missing_symbols = [symbol for symbol in symbols if symbol not in existing_symbols]
    missing_history = (
        _fetch_table(table, missing_symbols, capset, latest_only=False, **fetch_kwargs)
        if missing_symbols
        else pl.DataFrame()
    )
    current_symbols = [symbol for symbol in symbols if symbol in existing_symbols]
    latest = _fetch_table(table, current_symbols, capset, latest_only=True, **fetch_kwargs)
    # 历史行只用于合并，不能充当本次成功取数的证据或改写旧文件时间。
    if missing_history.is_empty() and latest.is_empty():
        return 0
    captured = _record_table(table, pl.concat([missing_history, latest], how="diagonal_relaxed"), data_dir)
    merged = _merge_report_history(existing, captured)
    return _write_table(table, merged, data_dir)


def sync_metrics(data_dir: Path, capset: CapabilitySet) -> int:
    """同步核心财务指标 (metrics), 历史各期累积保留。"""
    symbols = _get_symbols(data_dir)
    return _sync_history_table_for_symbols("metrics", symbols, data_dir, capset)


def sync_income(data_dir: Path, capset: CapabilitySet) -> int:
    """同步利润表, 历史各期累积保留。"""
    symbols = _get_symbols(data_dir)
    return _sync_history_table_for_symbols("income", symbols, data_dir, capset)


def sync_balance_sheet(data_dir: Path, capset: CapabilitySet) -> int:
    """同步资产负债表, 历史各期累积保留。"""
    symbols = _get_symbols(data_dir)
    return _sync_history_table_for_symbols("balance_sheet", symbols, data_dir, capset)


def sync_cash_flow(data_dir: Path, capset: CapabilitySet) -> int:
    """同步现金流量表, 历史各期累积保留。"""
    symbols = _get_symbols(data_dir)
    return _sync_history_table_for_symbols("cash_flow", symbols, data_dir, capset)


def sync_shares(data_dir: Path, capset: CapabilitySet) -> int:
    """同步历史股本表。"""
    symbols = _get_symbols(data_dir)
    return _sync_history_table_for_symbols("shares", symbols, data_dir, capset)


def sync_all(data_dir: Path, capset: CapabilitySet) -> dict[str, int]:
    """同步所有财务表。返回 {table: rows}。"""
    if not capset.has(Cap.FINANCIAL) and not _financial_is_custom():
        logger.info("sync_all financials skipped: no FINANCIAL capability")
        return {}

    symbols = _get_symbols(data_dir)
    results: dict[str, int] = {}
    for table in FINANCIAL_SYNC_TABLES:
        results[table] = _sync_history_table_for_symbols(
            table, symbols, data_dir, capset
        )

    # 同步完成后注册 DuckDB 视图
    _refresh_financials_views(data_dir)

    return results


# ================================================================
# DuckDB 视图
# ================================================================

def _refresh_financials_views(data_dir: Path) -> None:
    """刷新财务表 DuckDB 视图 (在 DataStore.db 上注册)。"""
    d = data_dir.as_posix()
    views = {
        "financials_metrics": f"{d}/financials/metrics/*.parquet",
        "financials_income": f"{d}/financials/income/*.parquet",
        "financials_balance_sheet": f"{d}/financials/balance_sheet/*.parquet",
        "financials_cash_flow": f"{d}/financials/cash_flow/*.parquet",
        "financials_shares": f"{d}/financials/shares/*.parquet",
    }
    for name in views:
        out = data_dir / "financials" / name.replace("financials_", "") / "part.parquet"
        if not out.exists():
            continue
        # 视图注册需要由 DataStore 完成,这里只做日志
        logger.debug("financial parquet ready: %s (%d rows)", name, out.stat().st_size)


def get_financial_df(data_dir: Path, table: str) -> pl.DataFrame:
    """读取本地财务 Parquet。"""
    path = data_dir / "financials" / table / "part.parquet"
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as e:
        logger.warning("读取 financials/%s 失败: %s", table, e, exc_info=True)
        return pl.DataFrame()


# ================================================================
# 调度器
# ================================================================

class FinancialScheduler:
    """独立调度器: 每周同步 metrics, 财务表支持手动同步。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self._data_dir: Path | None = None
        self._capset: CapabilitySet | None = None
        self._lock = threading.Lock()
        self._last_sync: dict[str, str] = {}  # {table: iso_timestamp}
        # 手动同步(run_now)是否正在进行。前端据此显示"同步中"并防重复点击。
        self._is_syncing = False

    def start(self, data_dir: Path, capset: CapabilitySet, *, auto_schedule: bool = False) -> None:
        """初始化调度器，并按需启动周期同步后台任务。

        auto_schedule=False (默认): 仅初始化 (设置数据目录/能力 + 恢复 last_sync),
            供 /api/financials/sync/* 手动同步使用, 不启动自动调度。
        auto_schedule=True: 额外启动每周一次的利润表自动同步 (启动后 60s 首跑)。
        """
        # 先记录 data_dir/capset, 即使当前无 FINANCIAL 也保留引用:
        # 用户稍后在「设置」页升级到 Expert Key 时, update_capabilities() 会把新 capset
        # 推进来,trigger()/run_now() 才能用上 FINANCIAL。否则 _capset 永远是 None,
        # 即便 app.state.capabilities 已更新, 调度器仍报 "no FINANCIAL capability"。
        self._data_dir = data_dir
        self._capset = capset
        if not capset.has(Cap.FINANCIAL) and not _financial_is_custom():
            logger.info("FinancialScheduler skipped: no FINANCIAL capability")
            return
        # 从持久化恢复上次同步时间: 重启后前端仍能显示真实最后同步时间,而非"尚未同步"
        try:
            from app.services import preferences
            restored = {table: timestamp for table, timestamp in preferences.get_financial_sync_times().items()
                        if table in FINANCIAL_SYNC_TABLES}
            # 浏览表也会被候选股局部更新；文件修改时间不能证明全市场同步完成。
            self._last_sync = restored
            if self._last_sync:
                logger.info("FinancialScheduler restored last_sync: %s", list(self._last_sync.keys()))
        except Exception as e:  # noqa: BLE001
            logger.warning("restore financial_sync_times failed: %s", e)

        if not auto_schedule:
            # 仅初始化 (手动同步用), 不启动周期任务。
            logger.info("FinancialScheduler initialized (auto-schedule disabled; manual sync only)")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("FinancialScheduler started (auto-schedule enabled)")

    def _record_sync(self, table: str) -> None:
        """记录一张表的同步完成时间: 更新内存 + 持久化到 preferences.json。

        持久化确保即使重启,前端 /status 仍返回真实的最后同步时间,
        不会错误地显示"尚未同步"。
        """
        ts = datetime.now(UTC).isoformat()
        self._last_sync[table] = ts
        try:
            from app.services import preferences
            preferences.set_financial_sync_time(table, ts)
        except Exception as e:  # noqa: BLE001
            logger.warning("persist financial_sync_time(%s) failed: %s", e)

    def update_capabilities(self, capset: CapabilitySet) -> None:
        """刷新调度器持有的能力集。

        用户在「设置」页新增/清除 API Key 后, settings API 会重新探测能力并更新
        app.state.capabilities; 必须同步推给本调度器, 否则 trigger()/run_now() 仍读
        启动时的旧 capset, 即便 app.state 已含 FINANCIAL, 调度器仍报
        "no FINANCIAL capability" 而拒绝同步 (表现为前端「全部同步」按钮闪一下无动作)。
        """
        prev = self._capset
        self._capset = capset
        had = bool(prev) and prev.has(Cap.FINANCIAL)
        now = capset.has(Cap.FINANCIAL)
        if had != now:
            logger.info(
                "FinancialScheduler capabilities updated: FINANCIAL %s -> %s", had, now
            )

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("FinancialScheduler stopped")

    async def _run_loop(self) -> None:
        """每周执行一次 metrics 同步。"""
        try:
            while self._running:
                # 首次启动等 60s, 之后每 7 天执行一次
                await asyncio.sleep(60)
                if not self._running:
                    break

                # 沿用可选调度的周期，但只同步当前启用的利润表。
                try:
                    rows = sync_income(self._data_dir, self._capset)
                    if rows > 0:
                        self._record_sync("income")
                    logger.info("FinancialScheduler: income synced, %d rows", rows)
                except Exception as e:  # noqa: BLE001 — 独立定时财务任务失败留痕，继续调度而不阻断采集服务
                    logger.warning("FinancialScheduler: income sync failed: %s", e, exc_info=True)

                # 等待下一次 (7天)
                for _ in range(7 * 24 * 60):  # 每分钟检查一次 _running
                    if not self._running:
                        break
                    await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass

    def _run_body(self, table: str | None, on_progress=None,
                  cancel_event: threading.Event | None = None) -> dict[str, int]:
        """同步逻辑本体(不加锁,假设调用方已持有 _is_syncing)。

        table=None 同步当前启用的利润表；旧非利润表请求明确拒绝。
        每张表完成立即更新 last_sync,让前端轮询 /status 能看到进度递增。
        """
        if table is not None and table not in FINANCIAL_SYNC_TABLES:
            raise ValueError("当前财务采集仅支持利润表")
        if table and on_progress:
            rows = _sync_history_table_for_symbols(table, _get_symbols(self._data_dir), self._data_dir,
                                                   self._capset, on_progress, cancel_event=cancel_event)
            if rows > 0:
                self._record_sync(table)
            _refresh_financials_views(self._data_dir)
            return {table: rows}
        if table:
            fn = {
                "metrics": sync_metrics,
                "income": sync_income,
                "balance_sheet": sync_balance_sheet,
                "cash_flow": sync_cash_flow,
                "shares": sync_shares,
            }.get(table)
            if not fn:
                return {}
            rows = fn(self._data_dir, self._capset)
            if rows > 0:
                self._record_sync(table)
            return {table: rows}
        # 全部同步
        symbols = _get_symbols(self._data_dir)
        result: dict[str, int] = {}
        for t in FINANCIAL_SYNC_TABLES:
            result[t] = _sync_history_table_for_symbols(
                t, symbols, self._data_dir, self._capset
            )
            if result[t] > 0:
                self._record_sync(t)
        _refresh_financials_views(self._data_dir)
        return result

    def run_now(self, table: str | None = None, on_progress=None,
                *, cancel_event: threading.Event | None = None) -> dict[str, int]:
        """同步执行一次同步(阻塞调用线程)。

        ⚠ 全量同步需数分钟,务必在后台线程调用,不要直接在 HTTP 请求线程里阻塞,
        否则请求会长时间 pending 直至被浏览器/代理超时掐断(表现为"点击无反应")。
        HTTP 接口应调用 trigger() 立即返回,再让前端轮询 /status.syncing 看进度。

        用 _is_syncing 标志防并发:若已有同步在进行,本次直接跳过,
        避免重复请求拖慢服务端 / 触发上游限流。
        """
        if not self._capset or (not self._capset.has(Cap.FINANCIAL) and not _financial_is_custom()):
            return {}
        with self._lock:
            if self._is_syncing:
                logger.info("financial sync skipped: already running")
                return {"_skipped": 1}
            self._is_syncing = True
        try:
            return (self._run_body(table, on_progress, cancel_event)
                    if on_progress or cancel_event is not None else self._run_body(table))
        finally:
            with self._lock:
                self._is_syncing = False

    def trigger(self, table: str | None = None) -> dict[str, int]:
        """触发一次同步(非阻塞,立即返回)。

        在后台线程执行同步体,HTTP 请求无需等待。
        返回 {"started": True/False}:
          - False = 能力不足或已有同步在进行(被防并发跳过)
          - True  = 已在后台开始,前端应轮询 /status.syncing 观察进度

        ⚠ _is_syncing 在此处置 True(持锁),确保 trigger 返回时前端轮询
        /status 已能看到 syncing=True,无竞态窗口;同时防止快速重复点击
        启动多个后台线程。后台线程复用 _run_body 执行真正的同步逻辑。
        """
        if not self._capset or (not self._capset.has(Cap.FINANCIAL) and not _financial_is_custom()):
            return {"started": False, "reason": "no FINANCIAL capability"}
        with self._lock:
            if self._is_syncing:
                logger.info("financial sync trigger skipped: already running")
                return {"started": False, "reason": "already running"}
            # 持锁置位:保证 trigger 返回前 syncing 已为 True
            self._is_syncing = True

        def _bg() -> None:
            try:
                self._run_body(table)
            except Exception as e:
                logger.exception("background financial sync failed: %s", e)
            finally:
                with self._lock:
                    self._is_syncing = False

        t = threading.Thread(target=_bg, name="financial-sync", daemon=True)
        t.start()
        logger.info("financial sync triggered in background: table=%s", table or "all")
        return {"started": True}

    @property
    def is_syncing(self) -> bool:
        """手动同步是否正在进行(供 /status 返回,前端据此显示"同步中")。"""
        with self._lock:
            return self._is_syncing

    @property
    def last_sync(self) -> dict[str, str]:
        return dict(self._last_sync)


# 全局单例
financial_scheduler = FinancialScheduler()
