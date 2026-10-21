from __future__ import annotations

from concurrent.futures import CancelledError
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.jobs.daily_pipeline import _sync_recap_boards
from app.services import auction_benchmark, dragon_tiger, trading_day
from app.services.pipeline_jobs import JobCancelledError, JobStore
from app.services.pipeline_prefetch import PipelinePrefetch, PipelineProgress

DAY = date(2026, 10, 16)


@pytest.fixture(params=["auction", "dragon_tiger"])
def board(request, tmp_path, monkeypatch):
    name = request.param
    module, suffix = ((auction_benchmark, "auction_benchmark") if name == "auction"
                      else (dragon_tiger, "dragon_tiger"))
    other, other_suffix = ((dragon_tiger, "dragon_tiger") if name == "auction"
                           else (auction_benchmark, "auction_benchmark"))
    monkeypatch.setattr(trading_day, "is_trading_day", lambda **_: True)
    monkeypatch.setattr(other, f"sync_{other_suffix}", Mock(return_value={"state": "source_unavailable"}))
    payload = {"state": "ok", "requested_date": DAY.isoformat(), "trade_date": DAY.isoformat()}
    fetch = Mock(return_value=payload)
    commit = Mock(return_value=payload)
    monkeypatch.setattr(module, f"prepare_{suffix}", fetch)
    monkeypatch.setattr(module, f"commit_{suffix}", commit)
    store = JobStore(store_dir=tmp_path / "jobs")
    job_id, _ = store.create()
    store.start(job_id)
    progress = PipelineProgress(lambda *args, **kwargs: store.progress(job_id, *args, **kwargs))
    progress("sync_boards", 99, "更新榜单")
    pool = PipelinePrefetch(progress)
    case = SimpleNamespace(name=name, repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
                           payload=payload, fetch=fetch, commit=commit, pool=pool,
                           store=store, job_id=job_id, task_id=f"prefetch_{name}")
    yield case
    pool.close()


def prefetch(case):
    case.pool.submit(case.task_id, "预取榜单", lambda: case.fetch(case.repo.store.data_dir, DAY))


def test_prefetch_failure_is_retried_once_at_formal_stage_with_fixed_target_day(board):
    board.fetch.side_effect = [ValueError("上游尚未发布"), board.payload]
    prefetch(board)
    result = _sync_recap_boards(board.repo, prefetch=board.pool, target_date=DAY)
    assert result[board.name]["state"] == "ok"
    assert board.fetch.call_count == 2
    assert [call.args[1] for call in board.fetch.call_args_list] == [DAY, DAY]
    board.commit.assert_called_once_with(board.payload, board.repo.store.data_dir)
    tasks = board.store.get(board.job_id)["tasks"]
    assert tasks[board.task_id]["status"] == "failed"
    assert tasks[f"sync_{board.name}"]["status"] == "succeeded"


@pytest.mark.parametrize("state", ["ok", "source_unavailable"])
def test_successful_or_unconfigured_prefetch_does_not_fetch_again(board, state):
    board.payload["state"] = state
    prefetch(board)
    result = _sync_recap_boards(board.repo, prefetch=board.pool)
    assert result[board.name]["state"] == state
    board.fetch.assert_called_once()
    board.commit.assert_called_once()


def test_failed_formal_retry_does_not_commit_or_hide_error(board):
    board.fetch.side_effect = [ValueError("尚未发布"), ValueError("正式阶段仍未发布")]
    prefetch(board)
    result = _sync_recap_boards(board.repo, prefetch=board.pool)
    assert result[board.name] == {"state": "error", "error": "正式阶段仍未发布"}
    assert board.fetch.call_count == 2
    board.commit.assert_not_called()


def test_commit_failure_never_retries_network_fetch(board):
    board.commit.side_effect = OSError("写入失败")
    prefetch(board)
    result = _sync_recap_boards(board.repo, prefetch=board.pool)
    assert result[board.name] == {"state": "error", "error": "写入失败"}
    board.fetch.assert_called_once()
    board.commit.assert_called_once()


def test_user_cancellation_after_failed_prefetch_does_not_start_retry(board):
    board.fetch.side_effect = ValueError("尚未发布")
    prefetch(board)
    with pytest.raises(ValueError):
        board.pool._futures[board.task_id].result(timeout=5)
    board.store.terminate(board.job_id, "用户手动取消")
    with pytest.raises(JobCancelledError):
        _sync_recap_boards(board.repo, prefetch=board.pool)
    board.fetch.assert_called_once()
    board.commit.assert_not_called()


def test_cancellation_during_formal_retry_prevents_commit(board):
    def retry(*_):
        if board.fetch.call_count == 1:
            raise ValueError("尚未发布")
        board.store.terminate(board.job_id, "用户手动取消")
        return board.payload

    board.fetch.side_effect = retry
    prefetch(board)
    with pytest.raises(JobCancelledError):
        _sync_recap_boards(board.repo, prefetch=board.pool)
    assert board.fetch.call_count == 2
    board.commit.assert_not_called()


def test_closed_pool_does_not_retry_a_failed_or_cancelled_future(board):
    board.fetch.side_effect = CancelledError("预取已取消")
    prefetch(board)
    board.pool.close()
    with pytest.raises(JobCancelledError):
        _sync_recap_boards(board.repo, prefetch=board.pool)
    assert board.fetch.call_count <= 1
    board.commit.assert_not_called()
