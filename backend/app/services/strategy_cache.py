"""策略结果缓存 — 写入本地文件，供策略页面秒加载。

缓存结构:
  {
    "as_of": "2024-01-15",
    "results": { strategy_id: { total, as_of, rows } },
    "today_ever_matched": { strategy_id: [symbol, ...] },    // 今日曾命中 symbol 并集
    "today_ever_rows": { strategy_id: { symbol: row_data } },// 今日曾命中的完整行数据
    "updated_at": 1705324800000  # Unix ms
  }

文件路径: data/user_data/strategy_cache.json
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.services.path_locks import path_lock
from app.services.private_storage import private_path


def _json_default(obj: Any) -> Any:
    """处理 date/datetime 等 JSON 不认识的类型。"""
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


logger = logging.getLogger(__name__)

_CACHE_FILENAME = "strategy_cache.json"

# File ownership stays isolated; only the bounded memo index has a short global lock.
_memo_lock = threading.Lock()
_memo: OrderedDict = OrderedDict()
_MEMO_MAX_BYTES = 32 * 1024 * 1024


def _signature(path):
    stat = path.stat()
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


def _cache_path(data_dir: Path) -> Path:
    return private_path(data_dir, "user_data", _CACHE_FILENAME)


def _enriched_parquet_path(data_dir: Path, as_of: str) -> Path:
    """返回 enriched parquet 文件路径。"""
    return data_dir / "kline_daily_enriched" / f"date={as_of}" / "part.parquet"


def _get_enriched_mtime(data_dir: Path, as_of: str) -> float | None:
    """返回 enriched parquet 文件的 mtime (秒)。文件不存在返回 None。"""
    p = _enriched_parquet_path(data_dir, as_of)
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return None


def read_cache(data_dir: Path) -> dict | None:
    """读取策略缓存文件。返回 None 表示无缓存或读取失败。

    说明: 原先有 enriched mtime 过期校验 (数据文件变化 → 判过期返回 None),
    但在有实时行情的系统里, enriched parquet 每轮被刷新 → mtime 必然变化 →
    缓存被永久判死, 策略页读不到数据。且判过期后不触发重算, 只能让用户手动重跑,
    保护价值有限。故移除: 盘后缓存总能读出, 实时新鲜度由 /api/screener/cached
    端点叠加监控引擎的内存实时结果 (latest_strategy_results) 来保证。
    """
    with path_lock(_cache_path(data_dir)):
        return _read_cache_unlocked(data_dir)


def clear_all_user_caches(data_dir: Path) -> None:
    """Shared configuration changes invalidate every persisted user's result."""
    from app.services.auth import get_store
    from app.services.user_context import UserContext, user_scope
    auth = get_store(data_dir)
    offset = 0
    while True:
        page = auth.list_users(limit=500, offset=offset)
        for row in page['users']:
            with user_scope(UserContext(row['id'], row['role'], data_dir)):
                clear_cache(data_dir)
        offset += len(page['users'])
        if offset >= page['total'] or not page['users']:
            break


def clear_cache(data_dir: Path, strategy_id: str | None = None) -> None:
    """清除失效结果；参数更新只清对应策略，注册表重载仍清整份缓存。"""
    import traceback

    # 运维可见性: 策略页依赖本缓存秒加载, 被清空即整页回退到全量重算。
    # 记录调用链 (最近 5 帧), 排查"缓存莫名消失"类问题不需要复现现场。
    frames = traceback.extract_stack()[:-1]
    chain = " <- ".join(
        f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}:{f.name}" for f in frames[-5:]
    )
    logger.warning("策略缓存被清除, 调用链: %s", chain)
    path = _cache_path(data_dir)
    with path_lock(path):
        if strategy_id is None:
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".tmp").unlink(missing_ok=True)
        else:
            cached = _read_cache_unlocked(data_dir)
            if cached is None:
                return
            for key in ("results", "today_ever_matched", "today_ever_rows"):
                cached.get(key, {}).pop(strategy_id, None)
            cached["updated_at"] = int(time.time() * 1000)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(cached, ensure_ascii=False, default=_json_default), encoding="utf-8")
            os.replace(temporary, path)


def summarize(cached: dict, results: dict | None = None) -> dict:
    if results is None:
        results = cached.get("results") or {}
    summary, counts = {}, {}
    for sid, result in results.items():
        if not isinstance(result, dict):
            continue
        summary[sid] = {
            "total": int(result.get("total") or 0), "as_of": result.get("as_of"),
            "computed_at": result.get("computed_at"),
        }
        if isinstance(result.get("verification"), dict):
            summary[sid]["verification"] = {k: v for k, v in result["verification"].items() if k != "candidates"}
        if result.get("as_of") == cached.get("as_of"):
            current = {str(row["symbol"]) for row in result.get("rows") or []
                       if isinstance(row, dict) and row.get("symbol")}
            counts[sid] = len(set((cached.get("today_ever_rows") or {}).get(sid) or {}) | current)
    return {"as_of": cached.get("as_of"), "results": summary, "today_ever_counts": counts,
            "updated_at": cached.get("updated_at")}


def _snapshot_unlocked(data_dir):
    path = _cache_path(data_dir)
    try:
        sig = _signature(path)
        with _memo_lock:
            memo = _memo.get(path)
            if memo is not None and memo[0] == sig:
                _memo.move_to_end(path)
                return memo[1], memo[2]
        cached = json.loads(path.read_text(encoding="utf-8"))
        summary = summarize(cached)
        if _signature(path) == sig:
            with _memo_lock:
                _memo[path] = (sig, cached, summary)
                _memo.move_to_end(path)
                while len(_memo) > 32 or sum(item[0][2] for item in _memo.values()) > _MEMO_MAX_BYTES:
                    _memo.popitem(last=False)
        return cached, summary
    except FileNotFoundError:
        with _memo_lock:
            _memo.pop(path, None)
        return None, None
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        logger.warning("读取策略缓存失败: %s", exc)
        return None, None


def _read_cache_unlocked(data_dir: Path) -> dict | None:
    # No caller can mutate a shared cached snapshot.
    return copy.deepcopy(_snapshot_unlocked(data_dir)[0])


def read_summary(data_dir: Path, realtime: dict | None = None) -> dict:
    with path_lock(_cache_path(data_dir)):
        cached, summary = _snapshot_unlocked(data_dir)
        cached = cached or {"as_of": None, "results": {}, "updated_at": None}
        result = copy.deepcopy(summary or summarize(cached))
        if realtime:
            overlay = summarize(cached, realtime)
            result["results"].update(overlay["results"])
            for sid in realtime:
                result["today_ever_counts"].pop(sid, None)
            result["today_ever_counts"].update(overlay["today_ever_counts"])
            result["updated_at"] = int(time.time() * 1000)
        return result


def _rows_to_symbol_map(rows: list[dict]) -> dict[str, dict]:
    """将 rows 列表转为 {symbol: row_data} 映射。"""
    result: dict[str, dict] = {}
    for row in rows:
        sym = row.get("symbol")
        if sym:
            result[sym] = row
    return result


def write_cache(
    data_dir: Path,
    as_of: str,
    results: dict[str, Any],
    *,
    same_date_only: bool = False,
) -> None:
    """将策略结果写入缓存文件，同时更新今日曾命中集合。

    - 日期变更时重置 today_ever_matched 和 today_ever_rows
    - 同一天内合并 (并集) 之前曾命中的 symbol，并用最新行数据更新
    - 单策略刷新传 same_date_only，仅在锁内确认已有缓存同日时更新
    """
    path = _cache_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 整个 read-modify-write 持锁: 避免并发 write 丢更新, 也避免与 read_cache 撕裂
    with path_lock(path):
        _write_cache_locked(path, data_dir, as_of, results, same_date_only=same_date_only)


def _write_cache_locked(
    path: Path,
    data_dir: Path,
    as_of: str,
    results: dict[str, Any],
    *,
    same_date_only: bool = False,
) -> None:
    """持用户文件锁后的实际写入逻辑 (read-merge-write + 原子替换)。"""
    # 读取旧缓存 (已持锁, 走不重入的 _read_cache_unlocked)
    old = _read_cache_unlocked(data_dir)
    old_as_of = old.get("as_of") if old else None
    if same_date_only and old_as_of != as_of:
        return
    old_ever_rows: dict[str, dict[str, dict]] = old.get("today_ever_rows", {}) if old else {}

    merged_results = {**(old.get("results") or {}), **results} if old_as_of == as_of else results
    if old_as_of == as_of:
        # 普通只读重算不会重新更新候选财务；保留上次显式核验快照与时间。
        # 待核验只存在 verification，不进入下面的 rows/曾命中并集。
        for sid, result in results.items():
            previous = (old.get("results") or {}).get(sid) or {}
            if "verification" not in result and "verification" in previous:
                merged_results[sid] = {**result, "verification": previous["verification"]}

    # 当前命中的行数据 → symbol 映射
    current_row_maps: dict[str, dict[str, dict]] = {}
    for sid, r in results.items():
        current_row_maps[sid] = _rows_to_symbol_map(r.get("rows", []))

    if old_as_of and old_as_of == as_of and old_ever_rows:
        # 同一天: 合并 — 用当前行数据更新旧数据 (保持最新价格等)
        merged_rows: dict[str, dict[str, dict]] = {}
        all_keys = set(old_ever_rows.keys()) | set(current_row_maps.keys())
        for sid in all_keys:
            old_map = old_ever_rows.get(sid, {})
            cur_map = current_row_maps.get(sid, {})
            # 以旧数据为基础，用当前数据覆盖 (当前数据更新鲜)
            combined = {**old_map, **cur_map}
            merged_rows[sid] = combined
        today_ever_rows = merged_rows
    else:
        # 新的一天或首次写入
        today_ever_rows = current_row_maps

    # 从 ever_rows 提取 symbol 列表 (用于快速计数)
    today_ever_matched = {sid: sorted(maps.keys()) for sid, maps in today_ever_rows.items()}

    # enriched_mtime: 盘后缓存写入时记录 (向后兼容旧字段)。read_cache 已不再用它
    # 做过期校验, 实时新鲜度改由 /cached 端点叠加监控引擎内存结果保证。
    enriched_mtime = _get_enriched_mtime(data_dir, as_of)

    payload = {
        "as_of": as_of,
        "results": merged_results,
        "today_ever_matched": today_ever_matched,
        "today_ever_rows": today_ever_rows,
        "enriched_mtime": enriched_mtime,
        "updated_at": int(time.time() * 1000),
    }
    try:
        # 原子写: 先写临时文件再 os.replace, 避免读侧读到半写的 JSON
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=_json_default), encoding="utf-8")
        os.replace(tmp, path)
        total_rows = sum(len(r.get("rows", [])) for r in merged_results.values())
        total_ever = sum(len(v) for v in today_ever_matched.values())
        logger.info("策略缓存已写入: %s, %d 策略, %d 命中, %d 曾命中", as_of, len(merged_results), total_rows, total_ever)
    except Exception as e:  # noqa: BLE001
        logger.warning("写入策略缓存失败: %s", e)
