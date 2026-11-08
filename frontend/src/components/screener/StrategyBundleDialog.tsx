import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, type StrategyBundlePreview } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Modal } from '@/components/Modal'

export function StrategyBundleDialog({ strategies, onClose, onImported }: {
  strategies: { id: string; name: string; research_only?: boolean }[]
  onClose: () => void
  onImported: (strategyId: string) => void
}) {
  const available = strategies.filter(strategy => !strategy.research_only)
  const [selected, setSelected] = useState(available[0]?.id ?? '')
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<StrategyBundlePreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const qc = useQueryClient()
  async function inspect(next: File | null) {
    setFile(next); setPreview(null); setError('')
    if (!next) return
    if (next.size > 8 * 1024 * 1024) { setError('策略包超过8MB上限'); return }
    setBusy(true)
    try { setPreview(await api.strategyInspectBundle(next)) }
    catch (cause) { setError(cause instanceof Error ? cause.message : '预览失败') }
    finally { setBusy(false) }
  }
  async function download() {
    setBusy(true); setError('')
    try {
      const url = URL.createObjectURL(await api.strategyExportBundle(selected))
      const anchor = document.createElement('a')
      anchor.href = url; anchor.download = `tsp-strategy-${selected}.zip`; anchor.click()
      URL.revokeObjectURL(url)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '导出失败') }
    finally { setBusy(false) }
  }
  async function apply() {
    if (!file || !preview?.can_apply) return
    setBusy(true); setError('')
    try {
      const result = await api.strategyApplyBundle(file)
      const prefixes = new Set<string>([QK.screenerStrategies()[0], QK.strategyDetail('')[0], QK.screenerCachedResult('')[0],
        QK.factorLibrary('stock')[0], QK.factorColumns[0], QK.customSignals[0], QK.customSignalsOptions[0], QK.strategyLinkOptions()[0]])
      await qc.invalidateQueries({ predicate: query => prefixes.has(String(query.queryKey[0])) })
      onImported(result.strategy_id); onClose()
    } catch (cause) { setError(cause instanceof Error ? cause.message : '导入失败，未保留新增资源') }
    finally { setBusy(false) }
  }
  return <Modal onClose={onClose} panelClassName="w-[min(640px,calc(100vw-24px))] max-h-[85vh] overflow-y-auto rounded-card border border-border bg-surface shadow-xl" overlayClassName="fixed inset-0 z-[70] flex items-center justify-center bg-black/50" labelledBy="strategy-bundle-title">
    <div className="space-y-4 p-5">
      <div className="flex justify-between gap-3"><h2 id="strategy-bundle-title" className="text-sm font-semibold">策略包导入 / 导出</h2><button onClick={onClose} aria-label="关闭策略包">关闭</button></div>
      <p className="text-xs text-muted">包含当前账号的声明式叠加策略、依赖与调参。内置和服务器策略保留ID引用；不接受Python源码包，也不覆盖已有内容。</p>
      <div className="flex flex-wrap gap-2"><select aria-label="导出策略" value={selected} onChange={event => setSelected(event.target.value)} className="min-w-0 flex-1 rounded border border-border bg-surface p-2 text-xs">{available.map(strategy => <option key={strategy.id} value={strategy.id}>{strategy.name}</option>)}</select><button disabled={busy || !selected} onClick={() => void download()} className="rounded border border-border px-3 py-2 text-xs disabled:opacity-40">导出策略包</button></div>
      <label className="block space-y-2 border-t border-border pt-4 text-xs">选择策略包ZIP<input aria-label="选择策略包" type="file" accept=".zip,application/zip" disabled={busy} onChange={event => void inspect(event.target.files?.[0] ?? null)} className="block w-full text-xs" /></label>
      {busy && <p role="status" className="text-xs text-muted">正在处理策略包…</p>}
      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
      {preview && <section aria-label="策略包预览" className="space-y-2 text-xs">
        <h3 className="font-semibold">{preview.label}</h3>
        <ul className="max-h-48 space-y-1 overflow-y-auto">{preview.entries.map(entry => <li key={entry.path} className="flex justify-between gap-3"><span className="truncate">{entry.id} · {entry.kind}</span><span className={entry.action === 'conflict' ? 'text-danger' : 'text-muted'}>{({ create: '新建', reuse: '相同内容复用', conflict: '同ID冲突' })[entry.action]}</span></li>)}</ul>
        {!preview.entries.length && <p>仅引用本机已有策略，无新增文件。</p>}
        {preview.warnings.map((warning, index) => <p key={`warning-${index}`} className="text-muted">{warning}</p>)}
        {preview.missing_deps.map((issue, index) => <p key={`issue-${index}`} className="text-danger">{issue}</p>)}
        {!preview.can_apply && <p className="text-danger">请先补齐依赖或解决同ID冲突，再重新选择策略包。</p>}
        <button disabled={busy || !preview.can_apply} onClick={() => void apply()} className="rounded bg-accent px-3 py-2 text-white disabled:opacity-40">确认无覆盖导入</button>
      </section>}
    </div>
  </Modal>
}
