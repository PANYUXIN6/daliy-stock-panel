import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Key,
  CheckCircle2,
  AlertCircle,
  Loader2,
  HelpCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { useSettings } from '@/lib/useSharedQueries'
import { QK } from '@/lib/queryKeys'
import { tierStyle, tierBaseName, ALL_TIERS, TierTag } from '@/lib/capability-labels'

// ===== TickFlow 详情内嵌区块 (组合进数据源页的单一详情卡, 不再各自成卡) =====
// 档位变化会重塑能力矩阵候选 (按档位过滤) → Key/档位相关写操作统一连带失效。

/** 区块小标题 (详情卡内部的分节, 区别于页面级 section) */
function SectionHeading({ icon: Icon, title, badge, right }: {
  icon: React.ComponentType<{ className?: string }>
  title: string
  badge?: string
  right?: React.ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-2 mb-3">
      <div className="flex items-center gap-2 min-w-0">
        <Icon className="h-3.5 w-3.5 text-secondary shrink-0" />
        <h3 className="text-xs font-medium text-foreground">{title}</h3>
        {badge && (
          <span className="px-1.5 py-0.5 text-[10px] font-mono rounded bg-elevated text-muted shrink-0">{badge}</span>
        )}
      </div>
      {right}
    </div>
  )
}

/** Key/档位/能力矩阵/侧栏状态的统一连带失效 (档位变化重塑矩阵候选) */
export function useInvalidateTierRelated() {
  const qc = useQueryClient()
  return () => {
    qc.invalidateQueries({ queryKey: QK.settings })
    qc.invalidateQueries({ queryKey: QK.capabilities })
    qc.invalidateQueries({ queryKey: QK.capabilityMatrix })
    // 档位变化会改变实时行情模式(none/watchlist/full_market), 立即刷新侧边栏状态
    qc.invalidateQueries({ queryKey: QK.quoteStatus })
  }
}

/** 密钥由后端配置，网页只显示状态并按需重新检测能力。 */
export function TickFlowKeySection({ right }: { right?: React.ReactNode }) {
  const settings = useSettings()
  const invalidate = useInvalidateTierRelated()
  const redetect = useMutation({
    mutationFn: () => api.redetectCapabilities(),
    onSuccess: () => invalidate(),
  })
  const configured = settings.data?.has_tickflow_key

  return (
    <div>
      <SectionHeading icon={Key} title="TickFlow API Key" right={right} />
      <p className="text-xs text-secondary leading-relaxed mb-4">
        密钥由后端管理。修改后端 .env 中的 TICKFLOW_API_KEY 并重启服务后生效。
      </p>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm" role="status">
          {settings.isLoading ? (
            <><Loader2 className="h-4 w-4 animate-spin" />读取中</>
          ) : settings.isError ? (
            <><AlertCircle className="h-4 w-4 text-danger" />配置状态读取失败</>
          ) : configured ? (
            <><CheckCircle2 className="h-4 w-4 text-bear" />后端已配置</>
          ) : (
            <><AlertCircle className="h-4 w-4 text-muted" />后端未配置</>
          )}
        </div>
        <button onClick={() => redetect.mutate()} disabled={redetect.isPending || !settings.data}
          className="px-3 py-1.5 rounded-btn bg-elevated text-secondary text-xs hover:text-accent disabled:opacity-50">
          {redetect.isPending ? '检测中…' : '重新检测'}
        </button>
      </div>
      {redetect.isError && (
        <p className="mt-3 text-xs text-danger" role="alert">检测失败，请检查后端配置与网络后重试。</p>
      )}
    </div>
  )
}

// ===== 档位说明弹窗 =====

export function TierHelpPopover({ currentLabel }: { currentLabel: string }) {
  const [open, setOpen] = useState(false)
  const currentBase = tierBaseName(currentLabel)

  return (
    <div className="relative inline-flex items-center">
      <HelpCircle
        className="h-4 w-4 text-muted/60 cursor-help hover:text-muted transition-colors"
        onClick={() => setOpen(v => !v)}
      />
      <AnimatePresence>
        {open && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
            <motion.div
              initial={{ opacity: 0, y: -4, scale: 0.95 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -4, scale: 0.95 }}
              transition={{ duration: 0.15 }}
              className="absolute top-full left-0 mt-1 z-50 w-72 bg-surface border border-border rounded-lg shadow-xl p-3.5 text-[11px] leading-relaxed"
              onClick={e => e.stopPropagation()}
            >
              {/* 档位 tag 横排 */}
              <div className="flex items-center gap-1.5 mb-3">
                {ALL_TIERS.map(t => (
                  <div key={t} className={`flex flex-col items-center gap-1 ${t === currentBase ? '' : 'opacity-60'}`}>
                    <TierTag label={t} />
                  </div>
                ))}
              </div>

              {/* 每档说明 */}
              <div className="space-y-1 mb-3 pb-3 border-b border-border">
                {ALL_TIERS.map(t => {
                  const s = tierStyle(t)
                  return (
                    <div key={t} className="flex items-center gap-2">
                      <span className="h-1.5 w-1.5 rounded-full shrink-0" style={s.dotStyle} />
                      <span className="font-mono font-bold w-12 shrink-0" style={s.labelTextStyle}>{t === 'none' ? 'None' : t}</span>
                      <span className="text-secondary">{s.desc}</span>
                    </div>
                  )
                })}
              </div>

              <div className="mb-3 rounded-btn border border-warning/30 bg-warning/10 px-2.5 py-1.5 text-[11px] font-medium text-warning">
                高等档位包含较低档位的全部权益。
              </div>

              {/* 检测说明 */}
              <div className="text-secondary space-y-1.5">
                <div className="font-medium text-foreground">档位检测说明</div>
                <p>配置后端 Key 后系统会在付费端点逐一试探数据能力:连单只日K都拿不到则判为「None」;有日K但无复权因子则判为「Free」;有复权因子再按代表能力判定 Starter/Pro/Expert。</p>
                <p className="text-muted">None 档与 Free 档运行时都走免费数据通道(仅历史日K),区别仅在于是否保存了 Key。付费档走付费端点,享有实时行情等完整能力。</p>
              </div>
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </div>
  )
}
