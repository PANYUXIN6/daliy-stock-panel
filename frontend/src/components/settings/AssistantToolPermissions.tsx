import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Loader2, LockKeyhole, RotateCcw, Save, Shield } from 'lucide-react'
import { api, type UserRole } from '@/lib/api'
import { useCurrentUser } from '@/lib/auth'
import { QK } from '@/lib/queryKeys'

const ROLE_LABELS: Record<UserRole, string> = { guest: '游客', pro: 'Pro', admin: '管理员' }
const ROLES: UserRole[] = ['guest', 'pro', 'admin']

export function AssistantToolPermissions() {
  const isAdmin = useCurrentUser()?.role === 'admin'
  const qc = useQueryClient()
  const query = useQuery({
    queryKey: QK.assistantToolPermissions,
    queryFn: () => api.assistantToolPermissions(),
    enabled: isAdmin,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  })
  const [draft, setDraft] = useState<Record<string, UserRole> | null>(null)
  const [saved, setSaved] = useState(false)
  const tools = query.data?.tools ?? []
  const permissions = draft ?? Object.fromEntries(tools.map(tool => [tool.name, tool.min_role]))
  const changed = tools.some(tool => permissions[tool.name] !== tool.min_role)
  const groups = tools.reduce((result, tool) => {
    const items = result.get(tool.group) ?? []
    items.push(tool)
    result.set(tool.group, items)
    return result
  }, new Map<string, typeof tools>())
  const counts = {
    guest: tools.filter(tool => permissions[tool.name] === 'guest').length,
    pro: tools.filter(tool => permissions[tool.name] !== 'admin').length,
    admin: tools.length,
  }
  const save = useMutation({
    mutationFn: (next: Record<string, UserRole>) => api.saveAssistantToolPermissions(next),
    onSuccess: data => {
      qc.setQueryData(QK.assistantToolPermissions, data)
      setDraft(null)
      setSaved(true)
    },
  })

  if (!isAdmin) return null

  const updateDraft = (next: Record<string, UserRole>) => {
    setDraft(next)
    setSaved(false)
    save.reset()
  }

  return (
    <section className="rounded-card border border-border bg-surface p-5" aria-labelledby="assistant-tool-permissions-title">
      <div className="mb-3 flex items-center gap-2.5">
        <Shield className="h-4 w-4 text-secondary" />
        <h2 id="assistant-tool-permissions-title" className="text-sm font-medium text-foreground">助手工具权限</h2>
      </div>
      <p className="text-xs leading-relaxed text-muted">
        选择每个工具的最低可用角色。Pro 包含游客权限，管理员始终可用全部工具。模型只会看到当前用户有权使用的工具，执行前还会再次校验。
      </p>

      {query.isLoading ? (
        <div role="status" className="flex items-center justify-center gap-2 py-8 text-xs text-muted">
          <Loader2 className="h-4 w-4 animate-spin" />正在加载工具权限…
        </div>
      ) : null}
      {query.isError ? (
        <div role="alert" className="mt-4 rounded-lg border border-danger/20 bg-danger/[0.04] p-3 text-xs text-danger">
          <p>工具权限加载失败：{query.error.message}</p>
          <button type="button" onClick={() => void query.refetch()} disabled={query.isFetching}
            className="mt-2 underline underline-offset-2 disabled:opacity-50">重新加载</button>
        </div>
      ) : null}
      {query.data?.configuration_error ? (
        <div role="alert" className="mt-4 rounded-lg border border-amber-400/20 bg-amber-400/[0.04] p-3 text-xs leading-relaxed text-amber-400">
          <p>工具权限配置异常：{query.data.configuration_error}</p>
          <p className="mt-1">可点击“恢复默认”检查默认权限，再保存以修复配置。</p>
        </div>
      ) : null}
      {query.isSuccess && tools.length === 0 ? <p className="py-8 text-center text-xs text-muted">暂无可配置的助手工具。</p> : null}

      {tools.length > 0 ? (
        <>
          <div className="my-4 flex flex-wrap gap-2" aria-label="当前草稿可用工具数量">
            {ROLES.map(role => (
              <div key={role} className="min-w-[5rem] flex-1 rounded-lg border border-border/30 bg-base/40 px-3 py-2.5">
                <div className="text-[11px] text-muted">{ROLE_LABELS[role]}</div>
                <div className="mt-1 text-sm font-medium tabular-nums text-foreground" aria-label={`${ROLE_LABELS[role]}可用 ${counts[role]} 个工具`}>
                  {counts[role]}<span className="ml-1 text-[11px] font-normal text-muted">个工具</span>
                </div>
              </div>
            ))}
          </div>
          <div className="space-y-5">
            {[...groups].map(([group, items]) => (
              <div key={group}>
                <h3 className="mb-2 text-[11px] font-medium text-secondary">{group}</h3>
                <div className="divide-y divide-border/20 rounded-lg border border-border/30">
                  {items.map(tool => (
                    <div key={tool.name} className="flex flex-col gap-3 p-3 sm:flex-row sm:items-center sm:justify-between">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                          <span className="text-xs font-medium text-foreground">{tool.label}</span>
                          {tool.requires_confirmation ? <span className="rounded bg-amber-400/10 px-1.5 py-0.5 text-[10px] text-amber-400">执行前需确认</span> : null}
                        </div>
                        <p className="mt-1 text-[11px] leading-relaxed text-muted">{tool.description}</p>
                      </div>
                      <div className="flex shrink-0 items-center gap-1.5 self-start sm:self-center">
                        {tool.locked ? <LockKeyhole className="h-3 w-3 text-muted" aria-hidden="true" /> : null}
                        <select aria-label={`${tool.label}最低可用角色`} value={permissions[tool.name]}
                          disabled={tool.locked || save.isPending}
                          onChange={event => updateDraft({ ...permissions, [tool.name]: event.target.value as UserRole })}
                          className="h-8 w-28 rounded-lg border-0 bg-base px-2 text-xs text-foreground ring-1 ring-border/30 focus:outline-none focus:ring-2 focus:ring-accent/30 disabled:cursor-not-allowed disabled:text-muted">
                          {ROLES.map(role => <option key={role} value={role}>{role === 'admin' ? '仅管理员' : `${ROLE_LABELS[role]}及以上`}</option>)}
                        </select>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
          {save.isError ? <p role="alert" className="mt-4 text-xs text-danger">保存失败：{save.error.message}</p> : null}
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-border/20 pt-4">
            <button type="button" disabled={save.isPending}
              onClick={() => updateDraft(Object.fromEntries(tools.map(tool => [tool.name, tool.default_min_role])))}
              className="inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-border/50 px-3 text-xs text-secondary transition-colors hover:border-accent/30 hover:text-accent disabled:opacity-40">
              <RotateCcw className="h-3.5 w-3.5" />恢复默认
            </button>
            <div className="flex flex-wrap items-center gap-3">
              <span role="status" className="text-[11px] text-muted">{changed ? '有未保存的修改' : saved ? '工具权限已保存' : '保存后生效'}</span>
              <button type="button" onClick={() => save.mutate(permissions)}
                disabled={save.isPending || (!changed && !query.data?.configuration_error)}
                className="inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-40">
                {save.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : saved ? <Check className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />}
                {save.isPending ? '保存中…' : '保存工具权限'}
              </button>
            </div>
          </div>
        </>
      ) : null}
    </section>
  )
}
