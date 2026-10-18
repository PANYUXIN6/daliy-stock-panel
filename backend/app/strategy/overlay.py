"""个人策略叠加条件：复用自定义信号语法，只约束入场候选。"""
from __future__ import annotations

import math

import numpy as np
import polars as pl

from app.strategy import custom_signals as cs
from app.strategy.scoring import scoring_dependencies, scoring_warmup_bars


def resolve(overrides: dict | None, backend: str | None = None) -> list[dict]:
    conditions = (overrides or {}).get('overlay_filter', [])
    if not isinstance(conditions, list):
        raise ValueError('overlay_filter 必须是条件数组')
    if not conditions:
        return []
    if backend in ('composite', 'minute_filter'):
        raise ValueError('当前策略不支持叠加条件，请在日线子策略中设置')
    # 复用信号的白名单、运算符和左右值类型校验；补上严格有限值/整数约束。
    try:
        cs.validate(dict(id='overlay', name='叠加条件', kind='entry', conditions=conditions))
        strings = cs._string_ext_fields()
        for c in conditions:
            for key in ('leftDays', 'rightDays'):
                value = c.get(key, 0)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError('叠加条件日期偏移必须是整数')
            kind, value = cs._parse_right(c['right'], string_mode=c['left'] in strings)
            if kind == 'const' and not math.isfinite(value):
                raise ValueError('叠加条件数值必须是有限数')
            if kind == 'field' and value in strings:
                raise ValueError('数值条件不能引用字符串字段')
            if backend == 'matrix_native' and c['left'] in strings:
                raise ValueError('矩阵策略暂不支持字符串叠加条件')
    except (TypeError, KeyError) as exc:
        raise ValueError('叠加条件格式错误') from exc
    ext_names = {name for name in dependencies(conditions) | fields(conditions) if name.startswith('ext_')}
    if ext_names:
        from app.config import settings
        from app.factors.ext_factors import ext_column_name
        from app.services.ext_data import ExtConfigStore
        historical = {
            ext_column_name(cfg.id, field.name)
            for cfg in ExtConfigStore(settings.data_dir).load_all() if cfg.mode == 'timeseries'
            for field in cfg.fields
        }
        if ext_names - historical:
            raise ValueError('叠加条件仅支持按交易日保存的时序扩展字段，不能使用最新快照回测')
    return conditions


def fields(conditions: list[dict]) -> set[str]:
    if not conditions:
        return set()
    names = set()
    strings = cs._string_ext_fields()
    for c in conditions:
        names.add(c['left'])
        kind, value = cs._parse_right(c['right'], string_mode=c['left'] in strings)
        if kind == 'field':
            names.add(value)
    return names


def dependencies(conditions: list[dict]) -> set[str]:
    return scoring_dependencies(dict.fromkeys(fields(conditions), 1))


def history_bars(conditions: list[dict]) -> int:
    if not conditions:
        return 1
    offset = max((c.get(key, 0) for c in conditions for key in ('leftDays', 'rightDays')), default=0)
    from app.indicators.pipeline import INDICATOR_COLUMNS
    needed = dependencies(conditions) | fields(conditions)
    # enriched 指标本身有历史值；矩阵路径从原始价补算时沿用回测 60 日暖机下限。
    indicator_bars = 61 if needed & set(INDICATOR_COLUMNS) else 1
    return offset + max(indicator_bars, scoring_warmup_bars(dict.fromkeys(fields(conditions), 1)))


def mask(frame: pl.DataFrame, conditions: list[dict]) -> pl.Series:
    """完整历史上求值，返回与输入等长同序的掩码；按标的实际交易行回看。"""
    try:
        return _mask(frame, conditions)
    except pl.exceptions.PolarsError as exc:
        raise ValueError(f'叠加条件依赖字段不足或类型不匹配: {exc}') from exc


def _mask(frame: pl.DataFrame, conditions: list[dict]) -> pl.Series:
    if not conditions:
        return pl.Series('_overlay', [True] * frame.height, dtype=pl.Boolean)
    work = frame.with_row_index('__overlay_row')
    if {'symbol', 'date'}.issubset(work.columns):
        work = work.sort(['symbol', 'date'])
    elif any(c.get(k, 0) for c in conditions for k in ('leftDays', 'rightDays')):
        raise ValueError('叠加条件日期偏移需要历史数据窗口')
    names = fields(conditions)
    if any(name.startswith('ext_') for name in dependencies(conditions) | names):
        from app.factors.ext_factors import attach_ext_columns
        work = attach_ext_columns(work, include_snapshot=False)
    # 矩阵回测只携带基础行情列；按需补同一指标流水线，不能另算近似公式。
    from app.indicators.pipeline import INDICATOR_COLUMNS, compute_indicators
    indicators = (dependencies(conditions) | names) & set(INDICATOR_COLUMNS) - set(work.columns)
    if indicators:
        work = compute_indicators(work, needed=indicators)
    work = cs.materialize_factor_columns(work, {'overlay': pl.all_horizontal([pl.col(n).is_not_null() for n in names])})
    missing = names - set(work.columns)
    if missing:
        raise ValueError('叠加条件引用的列不存在: ' + ', '.join(sorted(missing)))
    strings = cs._string_ext_fields()
    parts = []
    for c in conditions:
        left = cs._col(c['left'], c.get('leftDays', 0))
        kind, value = cs._parse_right(c['right'], string_mode=c['left'] in strings)
        right = cs._col(value, c.get('rightDays', 0)) if kind == 'field' else pl.lit(value)
        valid = left.is_not_null() & right.is_not_null()
        if c['left'] not in strings:
            left = left.cast(pl.Float64, strict=False)
            right = right.cast(pl.Float64, strict=False)
            valid = valid & left.is_finite() & right.is_finite()
        parts.append(valid & cs._OP_BUILDERS[c['op']](left, value if c['left'] in strings else right))
    return work.with_columns(pl.all_horizontal(parts).fill_null(False).alias('_overlay')).sort('__overlay_row')['_overlay']


def matrix_mask(market, conditions: list[dict]) -> np.ndarray:
    """仅展开依赖列和有效交易行，复用同一条件与因子计算口径。"""
    time_ids, asset_ids = np.nonzero(np.isfinite(market.close))
    data = {'symbol': np.asarray(market.symbols)[asset_ids],
            'date': np.asarray([label[:10] for label in market.timestamp_labels])[time_ids]}
    for name in dependencies(conditions) | fields(conditions) | {'open', 'high', 'low', 'close', 'volume'}:
        if name in market.fields or name in {'open', 'high', 'low', 'close', 'volume'}:
            data[name] = np.broadcast_to(market.field(name), market.shape)[time_ids, asset_ids]
    keep = mask(pl.DataFrame(data), conditions).to_numpy()
    result = np.zeros(market.shape, dtype=bool)
    result[time_ids, asset_ids] = keep
    return result
