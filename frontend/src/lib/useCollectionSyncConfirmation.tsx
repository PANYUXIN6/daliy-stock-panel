import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Modal } from '@/components/Modal'
import { api, type CollectionSyncRequest, type CollectionSyncCheck } from '@/lib/api'

/** 只读取本地同步证据；确认后仍使用调用方原有采集入口。 */
export function useCollectionSyncConfirmation(context = '') {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<CollectionSyncCheck | null>(null)
  const operation = useRef<{ context: string; onConfirm: () => void } | null>(null)
  const currentContext = useRef(context)
  currentContext.current = context
  const cancelRef = useRef<HTMLButtonElement>(null)
  const titleId = useId()

  const cancel = () => {
    operation.current = null
    setPending(null)
    setBusy(false)
  }

  useEffect(() => {
    cancel()
    setError(null)
    return () => { operation.current = null }
    // 每次切换表单或弹窗上下文，旧检查和旧确认均失效。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [context])

  const run = (request: CollectionSyncRequest, onConfirm: () => void) => {
    if (operation.current) return
    const next = { context, onConfirm }
    operation.current = next
    setBusy(true)
    setError(null)
    api.collectionSyncCheck(request).then(result => {
      if (operation.current !== next || currentContext.current !== next.context) return
      if (result.requires_confirmation) {
        setPending(result)
      } else {
        cancel()
        next.onConfirm()
      }
    }).catch((cause: Error) => {
      if (operation.current !== next || currentContext.current !== next.context) return
      cancel()
      setError(cause.message)
    })
  }

  const confirm = () => {
    const next = operation.current
    if (!next || currentContext.current !== next.context) return
    cancel()
    next.onConfirm()
  }

  const dialog = pending ? createPortal(
    <Modal onClose={cancel} labelledBy={titleId} initialFocusRef={cancelRef}
      overlayClassName="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      panelClassName="w-full max-w-md max-h-[88vh] overflow-y-auto bg-surface border border-border rounded-card shadow-xl p-5">
      <h3 id={titleId} className="text-sm font-medium text-foreground">是否重新拉取？</h3>
      <p className="mt-3 text-xs leading-relaxed text-secondary">{pending.message}</p>
      {pending.last_synced_at && <p className="mt-2 text-[11px] text-muted">上次同步：{new Date(pending.last_synced_at).toLocaleString('zh-CN')}</p>}
      <div className="mt-5 flex justify-end gap-2">
        <button ref={cancelRef} type="button" onClick={cancel}
          className="h-9 px-4 rounded-btn text-xs text-secondary hover:bg-elevated">取消</button>
        <button type="button" onClick={confirm}
          className="h-9 px-4 rounded-btn bg-accent text-xs font-medium text-white hover:bg-accent/90">重新拉取</button>
      </div>
    </Modal>, document.body,
  ) : null

  return { run, busy, error, dialog }
}
