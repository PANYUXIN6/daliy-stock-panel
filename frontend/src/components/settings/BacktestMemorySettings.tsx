import { useEffect, useState, type FormEvent } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export function BacktestMemorySettings({ value = 6 }: { value?: number }) {
  const qc = useQueryClient()
  const [gib, setGib] = useState(String(value))
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { setGib(String(value)) }, [value])

  const save = async (event: FormEvent) => {
    event.preventDefault()
    const amount = Number(gib)
    if (!gib.trim() || !Number.isInteger(amount) || amount < 0 || amount > 64) {
      setError('请输入 0–64 的整数。')
      return
    }
    setPending(true)
    setError('')
    try {
      await api.updateBacktestWorkerRssLimit(amount)
      await qc.invalidateQueries({ queryKey: QK.preferences })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '保存失败，请重试。')
    } finally {
      setPending(false)
    }
  }

  return <section className="rounded-card border border-border bg-surface p-5 mt-6">
    <h3 className="text-sm font-medium text-foreground">性能与内存</h3>
    <form onSubmit={save} className="mt-4 flex flex-wrap items-end gap-3">
      <label className="text-sm text-foreground">
        回测内存上限（GiB）
        <input type="number" min={0} max={64} step={1} required value={gib}
          onChange={event => setGib(event.target.value)} disabled={pending}
          className="mt-2 block h-8 w-28 rounded-btn border border-border bg-base px-2 text-sm" />
      </label>
      <button disabled={pending} className="rounded-btn bg-accent px-3 py-2 text-xs text-white disabled:opacity-50">
        {pending ? '保存中…' : '保存上限'}
      </button>
    </form>
    <p className="mt-2 text-xs text-muted">限制每个回测或优化任务的实际内存，超限会停止任务。0 表示不限制；修改对新任务生效，服务器部署配置优先。</p>
    {error && <p role="alert" className="mt-2 text-xs text-danger">{error}</p>}
  </section>
}
