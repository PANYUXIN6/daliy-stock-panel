"""_write_lock 锁区间瘦身的回归测试。

背景: polars 并发执行存在死锁风险 (app.polars_guard), 若 polars 读/合并/排序
悬死在 _write_lock 内, 全局写锁被永久持有 → 所有写路径排队冻结 (2026-09-07
线上全站冻结事故的放大器)。乐观并发模式把重活移到锁外, 此处验证:
- 并发 upsert 不丢行 (乐观重试的正确性);
- 合并计算期间 _write_lock 可被其他线程获取 (重活确实不在锁内)。
"""
from __future__ import annotations

import logging
import threading
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository


def _frame(symbols: list[str], dt: date = date(2026, 9, 7)) -> pl.DataFrame:
    n = len(symbols)
    return pl.DataFrame({
        "symbol": symbols,
        "date": [dt] * n,
        "close": [10.0 + i for i in range(n)],
    })


def test_concurrent_upserts_do_not_lose_rows(tmp_path: Path) -> None:
    repo = KlineRepository(DataStore(tmp_path))

    groups = [[f"{i:03d}{j:04d}.SZ" for j in range(8)] for i in range(6)]
    errors: list[BaseException] = []

    def worker(symbols: list[str]) -> None:
        try:
            for _ in range(3):  # 每线程多轮写, 提高乐观重试路径命中
                repo.merge_live_daily_asset("stock", _frame(symbols))
        except BaseException as exc:
            logging.getLogger(__name__).exception("concurrent daily writer failed")
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(g,), daemon=True) for g in groups]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert all(not t.is_alive() for t in threads)

    out = tmp_path / "kline_daily" / "date=2026-09-07" / "part.parquet"
    final = pl.read_parquet(out)
    assert final["symbol"].n_unique() == sum(len(g) for g in groups)  # 无一丢失
    assert final["symbol"].to_list() == sorted(final["symbol"].to_list())


@pytest.mark.parametrize("replace_during_read", [True, False], ids=["during-read", "after-read"])
def test_upsert_retries_when_partition_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace_during_read: bool,
) -> None:
    repo = KlineRepository(DataStore(tmp_path))
    repo.merge_live_daily_asset("stock", _frame(["000001.SZ"]))
    out = tmp_path / "kline_daily" / "date=2026-09-07" / "part.parquet"
    # 分别在读取元数据后、读取旧数据后插入另一写入者，稳定覆盖两个竞态窗口。
    reader_name = "scan_parquet" if replace_during_read else "read_parquet"
    original_reader = getattr(pl, reader_name)
    replaced = False
    concurrent_symbols = [f"{i:06d}.SZ" for i in range(2, 50)]

    def read_with_concurrent_replace(*args, **kwargs):
        nonlocal replaced
        result = original_reader(*args, **kwargs)
        if not replaced:
            replaced = True
            if replace_during_read:
                result.collect_schema()
            repo.merge_live_daily_asset("stock", _frame(concurrent_symbols))
        return result

    if replace_during_read:
        # read_parquet 在定义模块内调用 scan_parquet，不经过顶层 pl 导出。
        monkeypatch.setattr("polars.io.parquet.functions.scan_parquet", read_with_concurrent_replace)
    else:
        monkeypatch.setattr(pl, reader_name, read_with_concurrent_replace)
    try:
        repo.merge_live_daily_asset("stock", _frame(["000003.SZ", "000004.SZ"]))
        assert replaced
        final = pl.read_parquet(out)
        assert dict(final.select("symbol", "close").iter_rows()) == {
            **{symbol: 10.0 + i for i, symbol in enumerate(concurrent_symbols)},
            "000001.SZ": 10.0,
            "000003.SZ": 10.0,
            "000004.SZ": 11.0,
        }
    finally:
        repo.db.close()


def test_heavy_merge_runs_outside_write_lock(tmp_path: Path, monkeypatch) -> None:
    repo = KlineRepository(DataStore(tmp_path))
    # 预置旧分区内容, 让 upsert 走「读旧 + concat 合并」路径。
    repo.merge_live_daily_asset("stock", _frame(["000001.SZ"]))

    merge_entered = threading.Event()
    original_concat = pl.concat

    def slow_concat(*args, **kwargs):
        merge_entered.set()
        import time

        time.sleep(0.4)  # 模拟重合并耗时; 期间 _write_lock 必须是空闲的
        return original_concat(*args, **kwargs)

    monkeypatch.setattr(pl, "concat", slow_concat)

    done = threading.Event()

    def upsert() -> None:
        repo.merge_live_daily_asset("stock", _frame(["000002.SZ"]))
        done.set()

    t = threading.Thread(target=upsert, daemon=True)
    t.start()
    assert merge_entered.wait(timeout=5), "合并路径未被触发"

    acquired = repo._write_lock.acquire(timeout=1.0)
    assert acquired, "合并计算期间 _write_lock 被占用 — 重活仍在锁内"
    repo._write_lock.release()

    assert done.wait(timeout=5)
    t.join(timeout=5)
    out = tmp_path / "kline_daily" / "date=2026-09-07" / "part.parquet"
    assert pl.read_parquet(out)["symbol"].to_list() == ["000001.SZ", "000002.SZ"]
