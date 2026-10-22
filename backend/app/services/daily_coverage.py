"""本轮正式日线的当日覆盖证据；旧分区和实时快照不能代替本轮取数。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import polars as pl

from app.indicators.pipeline import filter_halt_days


class DailyCoverageError(RuntimeError):
    """当日正式行情或派生指标尚未齐全，不能发布盘后分析。"""


@dataclass
class DailyCoverage:
    day: date
    active: set[str] = field(default_factory=set)
    halted: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    not_listed: set[str] = field(default_factory=set)

    def observe(self, frame: pl.DataFrame) -> None:
        required = {"symbol", "date", "open", "high", "low", "close", "volume", "amount"}
        if frame.is_empty() or not required.issubset(frame.columns):
            return
        current = frame.filter(pl.col("date").cast(pl.Date) == self.day)
        if "quote_ts" in current.columns:
            current = current.filter(pl.col("quote_ts").is_null())
        # 与 filter_halt_days 同口径，但缺值不能被归类为停牌。
        halted = current.filter(
            ((pl.col("open") == 0) & (pl.col("high") == 0))
            | ((pl.col("volume") == 0) & (pl.col("amount") == 0))
        )
        self.halted.update(halted["symbol"].drop_nulls().to_list())
        active = filter_halt_days(current).filter(
            pl.all_horizontal(pl.col(c).is_finite() & (pl.col(c) > 0)
                              for c in ("open", "high", "low", "close"))
            & pl.all_horizontal(pl.col(c).is_finite() & (pl.col(c) >= 0)
                                for c in ("volume", "amount"))
        )
        self.active.update(active["symbol"].drop_nulls().to_list())

    def require(self, expected: list[str]) -> dict:
        missing = sorted(set(expected) - self.active - self.halted - self.not_listed)
        failed = sorted(set(expected) & self.failed)
        if missing or failed or not self.active:
            sample = ", ".join((missing or failed)[:10])
            raise DailyCoverageError(
                f"{self.day} 正式日线尚未齐全：缺少 {len(missing)} 只，"
                f"取数失败 {len(failed)} 只" + (f"（{sample}）" if sample else "")
            )
        result = {"date": self.day.isoformat(), "expected": len(set(expected)),
                  "active": len(set(expected) & self.active),
                  "halted": len(set(expected) & self.halted - self.active)}
        if self.not_listed:
            result["not_listed"] = len(set(expected) & self.not_listed - self.active - self.halted)
        return result
