"""短线投机阶段：基于连板生态的描述性规则，不代表全 A 市场或收益预测。

保留旧枚举供持久化/API 兼容，未命中规则为 unclassified，核心输入缺失为
unknown。阈值是项目历史经验参数，不是论文复现或样本外有效性证明。
"""
from __future__ import annotations

import logging
import math

import polars as pl

logger = logging.getLogger(__name__)

# ───────────────────────── 阶段词汇 ─────────────────────────
PHASE_ICE = "ice"
PHASE_IGNITE = "ignite"
PHASE_RALLY = "rally"
PHASE_CLIMAX = "climax"
PHASE_EBB = "ebb"
PHASE_REPAIR = "repair"
PHASE_UNCLASSIFIED = "unclassified"
PHASE_UNKNOWN = "unknown"

PHASE_LABELS = {
    PHASE_ICE: "短线低迷",
    PHASE_IGNITE: "短线启动",
    PHASE_RALLY: "短线活跃",
    PHASE_CLIMAX: "短线高热",
    PHASE_EBB: "短线退潮",
    PHASE_REPAIR: "短线修复",
    PHASE_UNCLASSIFIED: "未归类",
    PHASE_UNKNOWN: "数据不足",
}

# ───────────────────────── 项目经验阈值 ─────────────────────────
# 旧参数注记所述分位来自未入库标定，不构成可复现或样本外验证。
# 高热: 二板以上宽度或首板数达到项目经验阈值
CLIMAX_GE2 = 50            # p90(25) 的 2 倍
CLIMAX_FIRST_BOARD = 220   # p90(88) 的 2.5 倍
# 主升: 高度/宽度/晋级率同时高于中位 (p60), 或晋级率极强 (p85+)
RALLY_HEIGHT = 7           # p60
RALLY_GE2 = 15             # p60
RALLY_PROMO = 0.23         # p60
RALLY_PROMO_ALT = 0.30     # p85+
RALLY_GE2_ALT = 12
RALLY_HEIGHT_ALT = 5
# 退潮: 晋级率崩至 p20 以下且宽度自近期高位回落; 或晋级率/封板率双弱
EBB_PROMO = 0.15           # p20
EBB_PROMO_STRICT = 0.13
EBB_SEAL = 0.57            # ~p10-p15
EBB_RECENT_GE2 = 12        # 5 日前 ge2 高于此才认定"自高位退潮"
EBB_RECENT_HEIGHT = 6
# 启动: 宽度/高度自低位扩张且晋级率恢复
IGNITE_GE2_DELTA = 3       # ge2 较 5 日前增加量
IGNITE_GE2 = 8             # p20-p40
IGNITE_PROMO = 0.20        # ~p55
IGNITE_HEIGHT_DELTA = 1    # height 较 5 日前抬升
IGNITE_HEIGHT = 5          # p40
IGNITE_PROMO_SOFT = 0.19
# 冰点: 高度/宽度/首板同时贴地 (p10)
ICE_HEIGHT = 4             # p10-p20
ICE_GE2 = 6                # p10
ICE_FIRST_BOARD = 24       # p10

# 晋级率最小池(家数), 低于此记 null(小样本噪声)
PROMO_MIN_POOL = 10
# 平滑与持续性: EMA alpha≈1/3 (约 5 日), 阶段切换需连续 CONFIRM_DAYS 日同标签
_EMA_ALPHA = 1.0 / 3.0
_CONFIRM_DAYS = 2


def with_prev_consecutive(df: pl.DataFrame) -> pl.DataFrame:
    """按 symbol 追加昨日连板数列 _prev_consec (供晋级率)。

    df 需含 symbol/date/consecutive_limit_ups; 输入应覆盖前一交易日
    (调用方保证 warmup 或直接传全量), 每个符号首行 _prev_consec 为 null。
    """
    provenance = {"_prev_consec", "_prev_consec_date", "_prev_market_date"}
    if provenance.issubset(df.columns):
        # 仅复用本函数在 warmup 截断前生成的日期依据，并再次校验。
        return df.with_columns(
            pl.when(pl.col("_prev_consec_date") == pl.col("_prev_market_date"))
            .then(pl.col("_prev_consec")).otherwise(None).alias("_prev_consec")
        )
    df = df.drop([c for c in provenance if c in df.columns])
    dates = df.select("date").unique().sort("date").with_columns(
        pl.col("date").shift(1).alias("_prev_market_date")
    )
    return (
        df.sort(["symbol", "date"])
        .with_columns(
            pl.col("consecutive_limit_ups").shift(1).over("symbol").alias("_prev_consec"),
            pl.col("date").shift(1).over("symbol").alias("_prev_consec_date"),
        )
        .join(dates, on="date", how="left")
        .with_columns(
            pl.when(pl.col("_prev_consec_date") == pl.col("_prev_market_date"))
            .then(pl.col("_prev_consec")).otherwise(None).alias("_prev_consec")
        )
    )


def ladder_daily_aggs() -> list[pl.Expr]:
    """group_by("date").agg(...) 可直接拼接的梯队聚合表达式。

    要求 df 含 consecutive_limit_ups; 含 _prev_consec 时附带晋级率分子/分母。
    """
    consec = pl.col("consecutive_limit_ups")
    exprs = [
        consec.eq(1).sum().alias("first_board"),
        consec.ge(2).sum().alias("ge2_count"),
        consec.ge(3).sum().alias("ge3_count"),
        consec.ge(5).sum().alias("ge5_count"),
        consec.filter(consec.ge(2)).n_unique().alias("rungs_filled"),
    ]
    return [pl.when((consec.count() > 0) & (consec.null_count() == 0)).then(expr).otherwise(None).alias(expr.meta.output_name())
            for expr in exprs]


def ladder_promo_aggs() -> list[pl.Expr]:
    """晋级率聚合(分子/分母); 要求 df 已含 _prev_consec 列。"""
    prev = pl.col("_prev_consec")
    consec = pl.col("consecutive_limit_ups")
    comparable = prev.is_not_null() & consec.is_not_null()
    return [
        pl.when(comparable.any()).then((comparable & prev.ge(1)).sum())
        .otherwise(None).alias("promo_pool"),
        pl.when(comparable.any()).then((comparable & prev.ge(1) & consec.eq(prev + 1)).sum())
        .otherwise(None).alias("promo_ok"),
    ]


def finalize_ladder_row(r: dict) -> dict:
    """保留未覆盖字段的 null；真实零计数仍为零。"""
    def count(key):
        value = r.get(key)
        return int(value) if value is not None else None

    height, rungs = count("max_consecutive"), count("rungs_filled")
    completeness = rungs / (height - 1) if height is not None and height >= 3 and rungs is not None else None
    pool, ok = count("promo_pool"), count("promo_ok")
    promo = ok / pool if pool is not None and pool >= PROMO_MIN_POOL and ok is not None else None
    return {
        **{key: count(key) for key in ("first_board", "ge2_count", "ge3_count", "ge5_count")},
        "ladder_completeness": round(completeness, 4) if completeness is not None else None,
        "promo_pool": pool,
        "promo_rate": round(promo, 4) if promo is not None else None,
    }


def _ema(values: list[float | None], alpha: float = _EMA_ALPHA) -> list[float | None]:
    """因果 EMA；缺失不插值并重置，后续观测不能改写此前输出。"""
    out: list[float | None] = []
    cur = None
    for v in values:
        if v is None or not math.isfinite(v):
            cur = None
        else:
            cur = v if cur is None else cur + alpha * (v - cur)
        out.append(cur)
    return out


def classify_phase_series(daily: pl.DataFrame) -> pl.DataFrame:
    """对完整日序打阶段标签, 追加 phase 列。

    输入列: date, max_consecutive, first_board, ge2_count, promo_rate, seal_rate
    (promo_rate 允许 null)。处理: 各驱动因果 EMA 平滑（缺失重置） →
    逐日规则判定(按优先级) → 连续 _CONFIRM_DAYS 日同标签才切换(持续性)。
    """
    required = {"date", "max_consecutive", "first_board", "ge2_count", "promo_rate", "seal_rate"}
    missing = required - set(daily.columns)
    if missing:
        raise ValueError(f"classify_phase_series 缺少列: {sorted(missing)}")

    rows = daily.sort("date")
    n = rows.height
    height_s = _ema([float(v) if v is not None else None for v in rows["max_consecutive"].to_list()])
    first_s = _ema([float(v) if v is not None else None for v in rows["first_board"].to_list()])
    ge2_s = _ema([float(v) if v is not None else None for v in rows["ge2_count"].to_list()])
    promo_s = _ema([float(v) if v is not None else None for v in rows["promo_rate"].to_list()])
    seal_s = _ema([float(v) if v is not None else None for v in rows["seal_rate"].to_list()])

    def raw_label(i: int) -> str:
        h, fb, g2, pr, sr = height_s[i], first_s[i], ge2_s[i], promo_s[i], seal_s[i]
        if h is None or fb is None or g2 is None:
            return PHASE_UNKNOWN
        # 变化规则要求完整的 6 行窗口，不能把不足 5 日当成 5 日。
        history_ok = i >= 5 and all(
            x is not None for series in (height_s, ge2_s, promo_s)
            for x in series[i - 5:i + 1]
        )
        g2_prev = ge2_s[i - 5] if history_ok else None
        h_prev = height_s[i - 5] if history_ok else None
        # 高潮
        if g2 >= CLIMAX_GE2 or fb >= CLIMAX_FIRST_BOARD:
            return PHASE_CLIMAX
        # 主升
        if h >= RALLY_HEIGHT and g2 >= RALLY_GE2 and pr is not None and pr >= RALLY_PROMO:
            return PHASE_RALLY
        if pr is not None and pr >= RALLY_PROMO_ALT and g2 >= RALLY_GE2_ALT and h >= RALLY_HEIGHT_ALT:
            return PHASE_RALLY
        # 冰点: 高度/宽度/首板同时贴地 — 优先于退潮(持续死寂的市场是"短线低迷"
        # 而非"自高位退潮"; 退潮的规则 B 不带 from_high 条件, 顺序反了会把
        # 长期冰点误标成退潮)
        if h <= ICE_HEIGHT and g2 <= ICE_GE2 and fb <= ICE_FIRST_BOARD:
            return PHASE_ICE
        # 退潮: 自高位回落 + 晋级率坍塌, 或晋级/封板双弱
        from_high = history_ok and (g2_prev >= EBB_RECENT_GE2 or h_prev >= EBB_RECENT_HEIGHT)
        if from_high and (pr <= EBB_PROMO and g2 < g2_prev):
            return PHASE_EBB
        if pr is not None and sr is not None and pr <= EBB_PROMO_STRICT and sr <= EBB_SEAL:
            return PHASE_EBB
        # 启动: 自低位扩张
        if history_ok and g2 - g2_prev >= IGNITE_GE2_DELTA and g2 >= IGNITE_GE2 and pr >= IGNITE_PROMO:
            return PHASE_IGNITE
        if history_ok and h - h_prev >= IGNITE_HEIGHT_DELTA and h >= IGNITE_HEIGHT and pr >= IGNITE_PROMO_SOFT:
            return PHASE_IGNITE
        if history_ok and g2 > g2_prev and pr > promo_s[i - 5]:
            return PHASE_REPAIR
        return PHASE_UNCLASSIFIED

    labels: list[str] = []
    reasons: list[str] = []
    candidates: list[str | None] = []
    counts: list[int] = []
    current = PHASE_UNCLASSIFIED
    pending: str | None = None
    pending_run = 0
    for i in range(n):
        raw = raw_label(i)
        if raw in {PHASE_UNKNOWN, PHASE_UNCLASSIFIED}:
            # 无证据时立即撤销旧阶段，不能沿用昨天的修复/活跃。
            current = raw
            pending, pending_run = None, 0
            labels.append(raw)
            reasons.append("missing_core_inputs" if raw == PHASE_UNKNOWN else "no_rule_match")
            candidates.append(None)
            counts.append(0)
            continue
        if current == PHASE_UNKNOWN:
            current = PHASE_UNCLASSIFIED
        if raw == current:
            labels.append(current)
            pending, pending_run = None, 0
            reasons.append("confirmed")
            candidates.append(None)
            counts.append(0)
            continue
        if raw == pending:
            pending_run += 1
        else:
            pending, pending_run = raw, 1
        reasons.append("confirmed" if pending_run >= _CONFIRM_DAYS else "pending_confirmation")
        candidates.append(raw)
        counts.append(pending_run)
        if pending_run >= _CONFIRM_DAYS:
            current = raw
            labels.append(current)
            pending, pending_run = None, 0
        else:
            labels.append(current)
    return rows.with_columns(
        pl.Series("phase", labels, dtype=pl.Utf8).alias("phase"),
        pl.Series("phase_reason", reasons, dtype=pl.Utf8),
        pl.Series("phase_candidate", candidates, dtype=pl.Utf8),
        pl.Series("phase_candidate_count", counts, dtype=pl.Int64)
    ).sort("date")
