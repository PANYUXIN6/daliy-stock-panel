"""Parquet 原子替换的临时错误重试与失败传播测试。

replace_with_retry 对临时 PermissionError 短退避重试，耗尽后原样抛出。
repository 与 kline_sync 的原子写入共用这一契约。
"""
from __future__ import annotations

import os
from datetime import datetime

import polars as pl
import pytest

from app.services import kline_sync
from app.tickflow import repository


def _minute_frame() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600519.SH"],
        "datetime": [datetime(2026, 1, 15, 9, 30)],
        "open": [10.0], "high": [10.5], "low": [9.5], "close": [10.2],
        "volume": [100.0], "amount": [1020.0],
    })


def _flaky_replace(monkeypatch, fail_times: int) -> dict:
    """os.replace 前 fail_times 次 raise PermissionError, 之后正常执行。"""
    real_replace = os.replace
    state = {"calls": 0}

    def _flaky(src, dst):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise PermissionError(5, "拒绝访问。")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _flaky)
    return state


# ---------- replace_with_retry 本体 ----------

def test_retry_succeeds_after_transient_blocks(tmp_path, monkeypatch):
    out = tmp_path / "part.parquet"
    out.write_bytes(b"old")
    src = tmp_path / "part.parquet.tmp"
    src.write_bytes(b"new")
    state = _flaky_replace(monkeypatch, fail_times=2)

    repository.replace_with_retry(src, out, attempts=5, delay_s=0)

    assert out.read_bytes() == b"new"
    assert state["calls"] == 3
    assert not src.exists()


def test_retry_exhausted_raises_last_error(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError(5, "拒绝访问。")))
    src = tmp_path / "a.tmp"
    src.write_bytes(b"x")

    with pytest.raises(PermissionError, match="拒绝访问"):
        repository.replace_with_retry(src, tmp_path / "a.parquet", attempts=3, delay_s=0)
    assert src.exists()  # 未被消费, 目标未生成


def test_retry_no_block_single_attempt(tmp_path, monkeypatch):
    out = tmp_path / "part.parquet"
    out.write_bytes(b"old")
    src = tmp_path / "part.parquet.tmp"
    src.write_bytes(b"new")
    state = _flaky_replace(monkeypatch, fail_times=0)

    repository.replace_with_retry(src, out, attempts=5, delay_s=0)

    assert state["calls"] == 1
    assert out.read_bytes() == b"new"


# ---------- 两处 _atomic_write_parquet 接入 ----------

def test_kline_sync_atomic_write_survives_transient_lock(tmp_path, monkeypatch):
    state = _flaky_replace(monkeypatch, fail_times=1)
    out = tmp_path / "date=2026-01-15" / "part.parquet"
    out.parent.mkdir(parents=True)

    kline_sync._atomic_write_parquet(_minute_frame(), out)

    assert out.exists()
    assert state["calls"] == 2
    assert pl.read_parquet(out).height == 1


def test_repository_atomic_write_survives_transient_lock(tmp_path, monkeypatch):
    state = _flaky_replace(monkeypatch, fail_times=1)
    out = tmp_path / "kline_minute" / "date=2026-01-15" / "part.parquet"
    out.parent.mkdir(parents=True)

    repository.KlineRepository._atomic_write_parquet(_minute_frame(), out)

    assert out.exists()
    assert state["calls"] == 2


def test_write_minute_partition_survives_reader_race(tmp_path, monkeypatch):
    """集成: _write_minute_partition 读旧→concat→写新全程有读锁竞态仍完成。"""
    state = _flaky_replace(monkeypatch, fail_times=2)
    # 预置旧分区 (读改写路径)
    old_dir = tmp_path / "date=2026-01-15"
    old_dir.mkdir(parents=True)
    _minute_frame().write_parquet(old_dir / "part.parquet")

    written = kline_sync._write_minute_partition(_minute_frame(), tmp_path)

    assert written == 1
    assert state["calls"] >= 3  # 至少经历了重试
