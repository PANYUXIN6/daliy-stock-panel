/**
 * 概念/行业分析 — 共享 UI 组件
 *
 * 包含：
 * - AnalysisConfigDialog: 字段配置弹窗
 * - DimensionHeatmap: 维度热力图块
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import {
  ChevronDown,
  X,
} from 'lucide-react'
import { api, type ExtDataField } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import type { DimensionGroup, QuoteMap } from '@/lib/analysis-adapter'
import { computeQuoteMetrics } from '@/lib/analysis-adapter'
import { fmtPct, priceColorClass } from '@/lib/format'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

// ===== 配置类型 =====

export interface AnalysisFieldConfig {
  /** 选中的扩展数据源 ID */
  configId?: string
  /** 手动指定的维度字段（覆盖自动探测） */
  dimensionField?: string
  /** 层级字段统计级别（如行业 1/2/3 级） */
  hierarchyLevel?: 1 | 2 | 3
}

export interface SchemaOption {
  id: string
  label: string
  columns: { name: string; type: string; label: string }[]
}

// ===== 配置弹窗 =====

export function AnalysisConfigDialog({
  currentConfig,
  onSave,
  onClose,
  showHierarchyLevel = false,
}: {
  currentConfig: AnalysisFieldConfig
  onSave: (config: AnalysisFieldConfig) => void
  onClose: () => void
  showHierarchyLevel?: boolean
}) {
  const [draft, setDraft] = useState<AnalysisFieldConfig>(currentConfig)
  const backdrop = useDialogBackdrop(onClose)
  const { data: extList } = useQuery({
    queryKey: QK.extData,
    queryFn: api.extDataList,
  })
  const { data: schemaData } = useQuery({
    queryKey: QK.extDataSchemaAll,
    queryFn: api.extDataSchemaAll,
  })

  const configs = extList?.items ?? []

  const fieldsForConfig = useMemo((): ExtDataField[] => {
    if (!draft.configId || !schemaData?.items) return []
    const schema = schemaData.items.find(s => s.id === draft.configId)
    return schema?.columns.map(c => ({ name: c.name, dtype: c.type, label: c.label })) ?? []
  }, [draft.configId, schemaData])

  const nonMetaFields = fieldsForConfig.filter(
    f => !['symbol', 'code', 'date', 'name'].includes(f.name),
  )

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" {...backdrop}>
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="bg-surface border border-border rounded-lg shadow-xl w-[420px]"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 pt-3 pb-2">
          <span className="text-sm font-medium">配置数据源</span>
          <button onClick={onClose} className="p-0.5 text-muted hover:text-foreground">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="px-4 pb-4 space-y-3">
          {/* 数据源选择 */}
          <div className="space-y-1.5">
            <span className="text-xs text-secondary">扩展数据源</span>
            <select
              value={draft.configId ?? ''}
              onChange={e => setDraft(d => ({ ...d, configId: e.target.value || undefined, dimensionField: undefined }))}
              className="w-full h-8 bg-elevated border border-border rounded text-xs text-foreground px-2 focus:outline-none focus:border-accent/50"
            >
              <option value="">自动选择</option>
              {configs.map(c => (
                <option key={c.id} value={c.id}>{c.label}</option>
              ))}
            </select>
          </div>

          {/* 维度字段选择 */}
          {nonMetaFields.length > 0 && (
            <div className="space-y-1.5">
              <span className="text-xs text-secondary">维度字段（留空自动探测）</span>
              <select
                value={draft.dimensionField ?? ''}
                onChange={e => setDraft(d => ({ ...d, dimensionField: e.target.value || undefined }))}
                className="w-full h-8 bg-elevated border border-border rounded text-xs text-foreground px-2 focus:outline-none focus:border-accent/50"
              >
                <option value="">自动探测</option>
                {nonMetaFields.map(f => (
                  <option key={f.name} value={f.name}>{f.label || f.name}</option>
                ))}
              </select>
            </div>
          )}

          {showHierarchyLevel && (
            <div className="space-y-1.5">
              <span className="text-xs text-secondary">统计层级</span>
              <select
                value={draft.hierarchyLevel ?? 2}
                onChange={e => setDraft(d => ({ ...d, hierarchyLevel: Number(e.target.value) as 1 | 2 | 3 }))}
                className="w-full h-8 bg-elevated border border-border rounded text-xs text-foreground px-2 focus:outline-none focus:border-accent/50"
              >
                <option value={1}>一级行业</option>
                <option value={2}>二级行业（默认）</option>
                <option value={3}>三级行业</option>
              </select>
            </div>
          )}

          <div className="text-[10px] text-muted leading-relaxed">
            系统自动检测数据结构：支持"个股→概念/行业"和"概念/行业→成分股列表"两种模式。
            {showHierarchyLevel ? ' 行业字段按 “-” 拆分为 1/2/3 级。' : ''}
          </div>
        </div>

        <div className="flex justify-end gap-2 px-4 py-3 border-t border-border">
          <button onClick={onClose} className="px-3 py-1.5 text-xs text-secondary hover:text-foreground">取消</button>
          <button
            onClick={() => { onSave(draft); onClose() }}
            className="px-3 py-1.5 text-xs bg-accent/15 text-accent rounded hover:bg-accent/25"
          >
            保存
          </button>
        </div>
      </motion.div>
    </div>
  )
}

// ===== 维度热力图 =====

export function DimensionHeatmap({
  groups,
  quoteMap,
  selectedKey,
  onSelect,
  colorScheme,
}: {
  groups: DimensionGroup[]
  quoteMap: Map<string, QuoteMap>
  selectedKey: string | null
  onSelect: (key: string | null) => void
  colorScheme: 'blue' | 'amber'
}) {
  const [showAll, setShowAll] = useState(false)
  // 收起时最大高度（约 3 行标签高度）
  const collapsedMaxH = '8rem'

  const enriched = useMemo(() => {
    return groups.map(g => {
      const qm = computeQuoteMetrics(g.stocks, quoteMap)
      return { ...g, quoteMetrics: qm }
    })
  }, [groups, quoteMap])

  // 根据涨跌比渲染颜色强度
  const colors = colorScheme === 'blue'
    ? { up: [59, 130, 246], down: [96, 165, 250], bg: [30, 64, 175] }
    : { up: [245, 158, 11], down: [251, 191, 36], bg: [180, 83, 9] }

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs text-muted">热度分布（按标的覆盖数）</span>
        <button
          onClick={() => setShowAll(v => !v)}
          className="text-[10px] text-muted hover:text-foreground flex items-center gap-0.5"
        >
          {showAll ? '收起' : `展开全部 (${enriched.length})`}
          <ChevronDown className={`h-3 w-3 transition-transform ${showAll ? 'rotate-180' : ''}`} />
        </button>
      </div>
      <div
        className="flex flex-wrap gap-1.5 overflow-hidden transition-[max-height] duration-200"
        style={{ maxHeight: showAll ? 'none' : collapsedMaxH }}
      >
        {enriched.map(g => {
          const qm = g.quoteMetrics
          const total = qm.upCount + qm.downCount + qm.flatCount
          const upRatio = total > 0 ? qm.upCount / total : 0.5
          const size = Math.max(0.6, Math.min(1.4, g.count / (enriched[0]?.count || 1)))
          const active = selectedKey === g.key
          const [r, gr, b] = colors.up

          return (
            <button
              key={g.key}
              onClick={() => onSelect(active ? null : g.key)}
              className="px-2.5 py-1.5 rounded-sm text-[11px] whitespace-nowrap cursor-pointer hover:brightness-125 transition-all"
              style={{
                fontSize: `${10 + size * 2}px`,
                color: active ? '#fff' : `rgba(${r},${gr},${b},${0.6 + upRatio * 0.4})`,
                backgroundColor: active
                  ? `rgba(${r},${gr},${b},0.7)`
                  : `rgba(${r},${gr},${b},${0.08 + upRatio * 0.15})`,
                outline: active ? `1px solid rgba(${r},${gr},${b},0.8)` : 'none',
                outlineOffset: 1,
              }}
              title={`${g.key}: ${g.count}只, 涨${qm.upCount}/跌${qm.downCount}, 均幅${qm.avgPct != null ? fmtPct(qm.avgPct) : '—'}`}
            >
              {g.key}
              <span className="ml-1 opacity-60">{g.count}</span>
              {qm.avgPct != null && (
                <span className={`ml-1 ${priceColorClass(qm.avgPct)}`} style={{ fontSize: '9px' }}>
                  {fmtPct(qm.avgPct)}
                </span>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}
