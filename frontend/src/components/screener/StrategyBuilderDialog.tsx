import { Modal } from '@/components/Modal'
import type { DefaultStrategyBasicFilter } from '@/lib/storage'
import { ALL_BOARDS } from './StrategySettingsDialog'

/** 渲染 basic_filter 的 Python 字面量段 (缩进 8 空格, 与模板 META 对齐) */
export function renderBasicFilterPy(bf: DefaultStrategyBasicFilter): string {
  const py = (v: number | boolean | null) => (v === null ? 'None' : v === true ? 'True' : v === false ? 'False' : String(v))
  const boards = bf.boards.length ? bf.boards : [...ALL_BOARDS]
  return [
    `        "price_min": ${py(bf.price_min)}, "price_max": ${py(bf.price_max)},`,
    `        "market_cap_min": None,`,
    `        "float_cap_min": ${py(bf.float_cap_min)}, "float_cap_max": ${py(bf.float_cap_max)},`,
    `        "amount_min": ${py(bf.amount_min)}, "amount_max": ${py(bf.amount_max)},`,
    `        "turnover_min": ${py(bf.turnover_min)}, "turnover_max": ${py(bf.turnover_max)},`,
    `        "exclude_st": ${py(bf.exclude_st)}, "exclude_new_days": 30,`,
    `        "boards": [${boards.map(b => `"${b}"`).join(', ')}],`,
  ].join('\n')
}

/** 自定义策略代码模板: basic_filter 段用「默认基础参数」填充 */
export function buildCustomTemplate(bf: DefaultStrategyBasicFilter): string {
  return `"""策略简短描述"""
import polars as pl

META = {
    "id": "custom_my_strategy",
    "name": "我的策略",
    "description": "策略描述",
    "tags": ["自定义"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {
${renderBasicFilterPy(bf)}
    },
    "params": [],
    "scoring": {
        "change_pct": 0.5, "vol_ratio_5d": 0.5,
    },
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "polars_expr"
ENTRY_SIGNALS = ["signal_n_day_high"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 20

RULES = """
1. 规则一
2. 规则二
3. 规则三
"""

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return (
        (pl.col("close") > pl.col("ma20"))
        & (pl.col("volume") > pl.col("vol_ma5") * 1.5)
    )
`
}

interface Props {
  open: boolean
  onClose: () => void
  onSavedId?: (id: string, researchOnly?: boolean) => void | Promise<void>
  mode?: 'create' | 'modify'
  existingStrategyIds?: ReadonlySet<string>
}

export function StrategyBuilderDialog(props: Props) {
  if (!props.open) return null
  return <Modal onClose={props.onClose} ariaLabel="策略代码仅服务器部署"><div className="p-6 text-sm text-secondary">网页不支持编写、导入或 AI 生成 Python 策略。请选择可信策略模板、配置个人参数或使用声明式公式。<button className="mt-4 block text-accent" onClick={props.onClose}>关闭</button></div></Modal>
}
