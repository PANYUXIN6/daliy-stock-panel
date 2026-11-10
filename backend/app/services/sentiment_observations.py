"""Descriptive daily observations. Historical ranks never include the observed day."""
from __future__ import annotations

import copy
import json
import math
from functools import lru_cache
from pathlib import Path

import polars as pl

from app.market_time import cn_now

VERSION = "observations-v1"
WINDOW = 500
RADAR_AXES = {"up_pct", "above_ma20_pct", "total_amount", "index_position_60", "limit_up_pct", "seal_rate"}
SPECS = [
    ("up_pct", "上涨比例", "breadth", "%"),
    ("down_pct", "下跌比例", "breadth", "%"),
    ("above_ma20_pct", "MA20上方比例", "breadth", "ratio"),
    ("above_ma60_pct", "MA60上方比例", "breadth", "ratio"),
    ("limit_up_pct", "涨停比例", "speculation", "%"),
    ("limit_down_pct", "跌停比例", "speculation", "%"),
    ("total_amount", "成交额", "activity", "CNY"),
    ("amount_ratio_5_20", "5日/20日成交额", "activity", "ratio"),
    ("index_position_60", "上证60日价格位置", "price", "ratio"),
    ("index_position_120", "上证120日价格位置", "price", "ratio"),
    ("index_drawdown_60", "上证60日回撤", "price", "ratio"),
    ("index_drawdown_120", "上证120日回撤", "price", "ratio"),
    ("index_rsi21", "上证RSI21（简单均值）", "price", "points"),
    ("limit_up", "涨停家数", "speculation", "count"),
    ("limit_down", "跌停家数", "speculation", "count"),
    ("touched_limit", "触板家数", "speculation", "count"),
    ("broken_rate", "炸板率", "speculation", "ratio"),
    ("seal_rate", "封板率", "speculation", "ratio"),
    ("max_consecutive", "最高连板", "speculation", "count"),
    ("first_board", "首板家数", "speculation", "count"),
    ("ge2_count", "二板以上", "speculation", "count"),
    ("promo_rate", "晋级率", "speculation", "ratio"),
    ("ladder_completeness", "梯队完整度", "speculation", "ratio"),
    ("margin_buy_ratio", "融资买入占比", "external", "ratio"),
    ("option_sentiment", "期权情绪", "external", "ratio"),
    ("industry_etf_flows", "行业ETF净申购", "external", "CNY"),
]


def finite(value):
    return float(value) if isinstance(value, (float, int)) and math.isfinite(value) else None


def filter_universe(df: pl.DataFrame, excluded_symbols=()) -> pl.DataFrame:
    if excluded_symbols and "symbol" in df.columns:
        df = df.filter(~pl.col("symbol").str.to_uppercase().is_in(list(excluded_symbols)))
    if "volume" in df.columns and "change_pct" in df.columns:
        df = df.filter(~((pl.col("volume") == 0) & pl.col("change_pct").is_finite() & (pl.col("change_pct") == 0)).fill_null(False))
    return df


def observations_series(rows: list[dict], target_date: str | None = None) -> list[dict]:
    now = cn_now()
    result = _observations_cached(json.dumps(rows, default=str, sort_keys=True), str(now.date()), now.hour >= 15)
    return copy.deepcopy([r for r in result if str(r["date"]) == target_date] if target_date else result)


@lru_cache(maxsize=8)
def _observations_cached(serialized: str, today: str, after_close: bool) -> list[dict]:
    return _build_observations(json.loads(serialized), today, after_close)


def _build_observations(rows: list[dict], today: str, after_close: bool) -> list[dict]:
    """Operate on small daily aggregates, before the API query interval is applied."""
    rows = sorted({str(r["date"]): dict(r) for r in rows}.values(), key=lambda r: str(r["date"]))
    history = {key: [] for key, *_ in SPECS}
    streaks = dict.fromkeys(history, 0)
    last_sides = dict.fromkeys(history, None)
    for i, row in enumerate(rows):
        current = row.get("method_version") == VERSION
        complete = row.get("is_complete_day") is True and (str(row["date"]) < today or (str(row["date"]) == today and after_close))
        amount_window = rows[max(0, i - 19):i + 1]
        same_method = [r for r in amount_window
                       if r.get("method_version") == VERSION and r.get("exclude_st") == row.get("exclude_st")
                       and r.get("is_complete_day") is True
                       and (str(r["date"]) < today or (str(r["date"]) == today and after_close))]
        amounts = [finite(r.get("total_amount")) for r in same_method]
        row["amount_ratio_5_20"] = (sum(amounts[-5:]) / 5 / (sum(amounts) / 20)
                                       if len(amounts) == 20 and all(v is not None for v in amounts) and sum(amounts) > 0 else None)
        amount_reason = None
        if row["amount_ratio_5_20"] is None:
            valid_amount_days = sum(v is not None for v in amounts)
            problems = []
            if len(amount_window) < 20:
                problems.append(f"历史仅有{len(amount_window)}日")
            incompatible = [str(r["date"]) for r in amount_window
                            if r.get("method_version") != VERSION or r.get("exclude_st") != row.get("exclude_st")]
            unclosed = [str(r["date"]) for r in amount_window
                        if r.get("is_complete_day") is not True or str(r["date"]) > today
                        or (str(r["date"]) == today and not after_close)]
            missing_amount = [str(r["date"]) for r in same_method if finite(r.get("total_amount")) is None]
            for dates, reason in ((incompatible, "口径不一致，需重算环境"),
                                  (unclosed, "收盘口径未确认，需补采正式日线后重算环境"),
                                  (missing_amount, "成交额缺失，需补齐源数据后重算环境")):
                if dates:
                    dates_text = "、".join(dates[:3]) + (f"等{len(dates)}日" if len(dates) > 3 else "")
                    problems.append(f"{dates_text}{reason}")
            if valid_amount_days == 20 and sum(amounts) <= 0:
                problems.append("20日成交额均值不大于零")
            amount_reason = f"需完整20日成交额，当前有效{valid_amount_days}/20日；" + "；".join(problems)
        observations = []
        for key, label, group, unit in SPECS:
            value = finite(row.get(key)) if current else None
            prior = history[key][-WINDOW:]
            valid = [v for v, st in prior if v is not None and st == row.get("exclude_st")]
            percentile = (sum(v < value for v in valid) + .5 * sum(v == value for v in valid)) / WINDOW * 100 if value is not None and len(valid) == WINDOW and complete else None
            side = "low" if percentile is not None and percentile <= 10 else "high" if percentile is not None and percentile >= 90 else None
            streaks[key] = streaks[key] + 1 if side and side == last_sides[key] else int(side is not None)
            last_sides[key] = side
            old = prior[-5][0] if len(prior) >= 5 and prior[-5][1] == row.get("exclude_st") else None
            denom_key = {"above_ma20_pct": "ma20_valid_count", "above_ma60_pct": "ma60_valid_count", "seal_rate": "touched_limit", "broken_rate": "touched_limit"}.get(key)
            denominator = row.get(denom_key) if denom_key else row.get("change_valid_count") if key in {"up_pct", "down_pct"} else row.get("total_count") if key in {"limit_up_pct", "limit_down_pct"} else None
            missing_reason = None
            if value is None:
                missing_reason = ("旧方法记录，需重算" if not current else amount_reason if key == "amount_ratio_5_20"
                                  else "尚未接入可用数据源" if group == "external" else "有效数据或分母不足")
            percentile_reason = None
            if percentile is None:
                percentile_reason = (missing_reason if value is None else "收盘口径尚未确认，需刷新或重算" if not complete
                                     else f"需当日之前500个有效交易日，当前有效{len(valid)}/500日")
            observations.append(dict(key=key, label=label, group=group, unit=unit, value=value,
                denominator=finite(denominator), missing_reason=missing_reason, percentile_reason=percentile_reason, status="available" if value is not None else "unavailable",
                percentile=percentile, history_sample_count=len(valid), history_coverage=len(valid) / WINDOW,
                change_5d=value - old if value is not None and old is not None else None,
                extreme_streak=streaks[key], extreme_side=side))
            history[key].append((value if complete else None, row.get("exclude_st")))
        row["sentiment"] = dict(version=VERSION, as_of=str(row["date"]),
            universe=dict(exclude_st=row.get("exclude_st"), suspension_rule="仅剔除成交量明确为零且涨跌幅明确为零的股票"),
            coverage=dict(sample_count=finite(row.get("total_count")), valid_change_count=finite(row.get("change_valid_count")), method_current=current, is_complete_day=complete, is_closed_day=complete, market_coverage_verified=False, completeness_note="来源收盘口径已确认，不证明全市场覆盖验收通过" if complete else "盘中或缺少收盘来源状态，需刷新或重算"), observations=observations,
            context=_context(observations) if current else [],
            limitations=["仅描述状态，不预测收益、反转或卖压耗尽", "分位使用当日之前500个交易日，不足窗口不出分位", "价格背景仅代表上证指数000001.SH", "ST过滤使用当前风险警示名单，历史身份可能有偏差", "融资、期权及行业ETF净申购数据未接入"] + ([] if current else ["旧方法记录须由既有环境重算流程更新"]))
    return rows


def _context(observations: list[dict]) -> list[str]:
    values = {o["key"]: o for o in observations}
    result = []
    up, down = values["up_pct"]["value"], values["down_pct"]["value"]
    if up is not None and down is not None:
        result.append("上涨占比高于下跌占比" if up > down else "上涨占比未高于下跌占比")
    rank = values["total_amount"]["percentile"]
    if rank is not None and (rank <= 10 or rank >= 90):
        result.append("成交额处于历史低分位" if rank <= 10 else "成交额处于历史高分位")
    change = values["above_ma20_pct"]["change_5d"]
    if change is not None and change != 0:
        result.append("MA20广度较5日前改善" if change > 0 else "MA20广度较5日前下降")
    return result


def observation_cache_key(data_dir: Path) -> tuple:
    from app.services import preferences, regime_builder
    path = regime_builder.regime_path(Path(data_dir).resolve())
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    except OSError:
        signature = None
    now = cn_now()
    return (str(path.parent.parent), signature, VERSION,
            preferences.get_sentiment_exclude_st(), str(now.date()), now.hour >= 15)


@lru_cache(maxsize=8)
def _file_observations(key: tuple) -> dict[str, dict]:
    from app.services import regime_builder
    data_dir, _, _, exclude_st, today, after_close = key
    rows = regime_builder.load_regime_history(Path(data_dir)).to_dicts()
    for row in rows:
        if row.get("exclude_st") != exclude_st:
            row["method_version"] = None
    return {str(row["date"]): row["sentiment"]
            for row in _build_observations(rows, today, after_close)}


def load_observation(data_dir: Path, target_date: str | None = None) -> dict | None:
    """Read one immutable cached daily result; copy only the selected observation."""
    values = _file_observations(observation_cache_key(data_dir))
    if not values:
        return None
    return copy.deepcopy(values.get(target_date or max(values)))


@lru_cache(maxsize=8)
def _previous_radar(key: tuple, before: str) -> dict | None:
    for day, observation in reversed(_file_observations(key).items()):
        coverage = observation["coverage"]
        if day >= before or not coverage["is_complete_day"] or not coverage["method_current"]:
            continue
        axes = [dict(key=item["key"], label=item["label"], percentile=item["percentile"])
                for item in observation["observations"]
                if item["key"] in RADAR_AXES and item["percentile"] is not None]
        if len(axes) == len(RADAR_AXES):
            return {"as_of": day, "axes": axes}
    return None


def load_previous_radar(data_dir: Path, before: str) -> dict | None:
    """Return the most recent drawable closing radar strictly before the requested day."""
    return copy.deepcopy(_previous_radar(observation_cache_key(data_dir), before))


def analysis_readiness(data_dir: Path, target_date: str | None = None) -> dict | None:
    observation = load_observation(data_dir, target_date)
    if observation is None or not observation["coverage"]["method_current"]:
        return None
    radar = [item for item in observation["observations"] if item["key"] in RADAR_AXES]
    return {
        "as_of": observation["as_of"],
        "is_complete_day": observation["coverage"]["is_complete_day"],
        "radar_ready": len(radar) == len(RADAR_AXES) and all(item["percentile"] is not None for item in radar),
        "radar_history_min": min((item["history_sample_count"] for item in radar), default=0),
        "radar_history_max": max((item["history_sample_count"] for item in radar), default=0),
    }


@lru_cache(maxsize=8)
def _current_observation(key: tuple, serialized_row: str) -> dict:
    from datetime import date

    from app.services import regime_builder
    data_dir, _, _, _, today, after_close = key
    row = json.loads(serialized_row)
    history = regime_builder.load_regime_history(Path(data_dir))
    prior = history.filter(pl.col("date") < date.fromisoformat(row["date"])).to_dicts() if not history.is_empty() else []
    return _build_observations([*prior, row], today, after_close)[-1]["sentiment"]


def observe_current(data_dir: Path, row: dict) -> dict:
    """Transient overview row: key only the new aggregate, never serialize history."""
    return copy.deepcopy(_current_observation(observation_cache_key(data_dir), json.dumps(row, default=str, sort_keys=True)))
