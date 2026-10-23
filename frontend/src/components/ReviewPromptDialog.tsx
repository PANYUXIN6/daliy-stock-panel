import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'

export function ReviewPromptDialog({ userId, onClose }: { userId: string; onClose: () => void }) {
  const qc = useQueryClient()
  const query = useQuery({ queryKey: QK.reviewPrompt(userId), queryFn: api.reviewPrompt, retry: false })
  const [draft, setDraft] = useState<string | null>(null)
  const text = draft ?? query.data?.custom ?? ''
  const limit = query.data?.max_chars ?? 32_000
  const length = Array.from(text).length
  const mutation = useMutation({
    mutationFn: api.updateReviewPrompt,
    onSuccess: data => {
      qc.setQueryData(QK.reviewPrompt(userId), data)
      toast(data.customized ? '已保存个人复盘提示词' : '已恢复默认提示词', 'success')
      onClose()
    },
  })
  const unavailable = !query.data || query.isError || query.isFetching || mutation.isPending
  return (
    <Modal ariaLabel="个人复盘提示词" onClose={() => { if (!mutation.isPending) onClose() }}
      panelClassName="w-[94vw] max-w-2xl max-h-[90dvh] overflow-y-auto rounded-card border border-border bg-surface p-4 shadow-xl sm:p-6">
      <h2 className="text-sm font-semibold text-foreground">个人复盘提示词</h2>
      <p className="mt-2 text-xs leading-relaxed text-secondary">
        仅用于你生成的复盘，不影响其他账号。留空使用默认模板；保存自定义内容后，不再跟随默认模板更新。保存后从下一次生成开始生效。
      </p>
      {query.isPending && <p role="status" className="mt-4 text-xs text-muted">正在读取提示词…</p>}
      {query.isError && <div role="alert" className="mt-4 text-xs text-warning">
        提示词读取失败。<button className="ml-2 text-accent" onClick={() => query.refetch()}>重试</button>
      </div>}
      {query.data && !query.isError && <>
        <label htmlFor="review-prompt-text" className="mt-4 block text-xs text-secondary">
          系统提示词 · {query.data.customized ? '已自定义' : '使用默认模板'}
        </label>
        <textarea id="review-prompt-text" value={text} onChange={event => setDraft(event.target.value)}
          disabled={unavailable} spellCheck={false}
          placeholder="留空使用默认模板，或先填入默认模板再修改"
          className="mt-2 h-[38dvh] min-h-40 w-full resize-y rounded-btn border border-border bg-base p-3 text-xs leading-relaxed text-foreground disabled:opacity-50" />
        <p className={`mt-1 text-xs ${length > limit ? 'text-warning' : 'text-muted'}`}>
          {length.toLocaleString()} / {limit.toLocaleString()} 字符
        </p>
      </>}
      {mutation.isError && <p role="alert" className="mt-2 text-xs text-warning">保存失败，请重试。草稿已保留。</p>}
      <div className="mt-4 flex flex-wrap justify-end gap-2 text-xs">
        <button disabled={unavailable} onClick={() => setDraft(query.data!.default)} className="rounded-btn border border-border px-3 py-2 disabled:opacity-50">填入默认模板</button>
        <button disabled={unavailable || !query.data?.customized} onClick={() => mutation.mutate('')} className="rounded-btn border border-border px-3 py-2 disabled:opacity-50">恢复默认</button>
        <button disabled={mutation.isPending} onClick={onClose} className="rounded-btn bg-elevated px-3 py-2 disabled:opacity-50">取消</button>
        <button disabled={unavailable || length > limit} onClick={() => mutation.mutate(text)} className="rounded-btn bg-accent px-3 py-2 text-white disabled:opacity-50">{mutation.isPending ? '保存中…' : '保存提示词'}</button>
      </div>
    </Modal>
  )
}
