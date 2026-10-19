import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type User, type UserRole } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'

function defaultProExpiry() {
  const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date())
  const date = new Date(`${today}T00:00:00Z`)
  date.setUTCDate(date.getUTCDate() + 30)
  return date.toISOString().slice(0, 10)
}

const input = 'w-full rounded-btn border border-border bg-base px-3 py-2 text-sm text-foreground'
export function SettingsUsersPanel() {
  const qc = useQueryClient()
  const [search, setSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const [eventUser, setEventUser] = useState('')
  const [eventOffset, setEventOffset] = useState(0)
  const [editing, setEditing] = useState<User | 'new' | null>(null)
  const [resetting, setResetting] = useState<User | null>(null)
  const [confirmation, setConfirmation] = useState<{ user: User; action: 'disable' | 'revoke' } | null>(null)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<UserRole>('guest')
  const [expires, setExpires] = useState('')
  const users = useQuery({ queryKey: QK.users(search, offset), queryFn: () => api.users(search, offset) })
  const events = useQuery({ queryKey: QK.loginEvents(eventUser, eventOffset), queryFn: () => api.loginEvents(eventUser, eventOffset) })
  const change = useMutation({ mutationFn: async (action: () => Promise<unknown>) => action(), onSuccess: () => { void qc.invalidateQueries({ queryKey: ['users'] }); void qc.invalidateQueries({ queryKey: ['login-events'] }); setEditing(null); setResetting(null); setConfirmation(null); setPassword('') } })
  const open = (user: User | 'new') => { change.reset(); setEditing(user); setUsername(user === 'new' ? '' : user.username); setRole(user === 'new' ? 'guest' : user.role); setExpires(user === 'new' ? '' : user.pro_expires_on ?? ''); setPassword('') }
  return <section className="space-y-6 min-w-0">
    <div><h2 className="text-base font-semibold text-foreground">用户管理</h2><p className="mt-1 text-xs text-muted">临时密码需由用户首次登录后更改。Pro 到期日按北京时间当天有效。</p></div>
    <div className="flex flex-wrap gap-2"><input aria-label="搜索用户" placeholder="搜索用户名" value={search} onChange={e => { setSearch(e.target.value); setOffset(0) }} className={`${input} max-w-xs`} /><button className="rounded-btn bg-accent px-4 py-2 text-sm text-white" onClick={() => open('new')}>创建用户</button></div>
    {users.isLoading ? <p className="text-muted">加载中…</p> : users.error ? <p role="alert" className="text-danger">{users.error.message}</p> : <div className="overflow-x-auto rounded-card border border-border"><table className="w-full min-w-[740px] text-left text-xs"><thead className="bg-elevated text-muted"><tr>{['用户 / 角色', '状态 / 到期日', '累计登录', '最近登录', '操作'].map(x => <th className="p-3" key={x}>{x}</th>)}</tr></thead><tbody>{users.data?.users.map(user => <tr key={user.id} className="border-t border-border text-secondary"><td className="p-3"><strong className="text-foreground">{user.username}</strong><div>{user.role}</div></td><td className="p-3">{user.is_active ? '启用' : '已禁用'}{user.must_change_password && ' · 待改密'}<div>{user.pro_expires_on ?? '—'}</div></td><td className="p-3">{user.login_count}</td><td className="p-3">{user.last_login_at ? new Date(user.last_login_at).toLocaleString('zh-CN') : '尚未登录'}<div>{user.last_login_ip}</div></td><td className="p-3"><div className="flex flex-wrap gap-2">{user.role === 'admin' ? <span className="text-muted">管理员账号受保护</span> : <><button onClick={() => open(user)} className="text-accent">编辑权限</button><button disabled={change.isPending} onClick={() => { if (user.is_active) { change.reset(); setConfirmation({ user, action: 'disable' }) } else change.mutate(() => api.updateUser(user.id, { is_active: true })) }}>{user.is_active ? '禁用' : '恢复'}</button><button onClick={() => { change.reset(); setResetting(user); setPassword('') }}>重置密码</button><button disabled={change.isPending} onClick={() => { change.reset(); setConfirmation({ user, action: 'revoke' }) }}>强制下线</button></>}<button onClick={() => { setEventUser(user.id); setEventOffset(0) }}>登录记录</button></div></td></tr>)}</tbody></table>{users.data?.total === 0 && <p className="p-6 text-sm text-muted">没有匹配的用户</p>}</div>}
    <div className="flex gap-3 text-xs text-secondary"><button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</button><span>共 {users.data?.total ?? 0} 人</span><button disabled={offset + 50 >= (users.data?.total ?? 0)} onClick={() => setOffset(offset + 50)}>下一页</button></div>
    {change.error && !editing && !resetting && !confirmation && <p role="alert" className="text-sm text-danger">{change.error.message}</p>}
    <div className="flex items-center gap-3"><h3 className="text-sm font-semibold text-foreground">登录记录（保留 90 天）</h3>{eventUser && <button className="text-xs text-accent" onClick={() => { setEventUser(''); setEventOffset(0) }}>查看全部</button>}</div>
    {events.error ? <p role="alert" className="text-danger">{events.error.message}</p> : <div className="overflow-x-auto"><table className="w-full min-w-[640px] text-left text-xs"><thead className="text-muted"><tr>{['时间', '用户名', 'IP', '结果', '浏览器 / 设备'].map(x => <th className="p-2" key={x}>{x}</th>)}</tr></thead><tbody>{events.data?.events.map(event => <tr key={event.id} className="border-t border-border text-secondary"><td className="p-2">{new Date(event.occurred_at).toLocaleString('zh-CN')}</td><td className="p-2">{event.username}</td><td className="p-2">{event.ip}</td><td className="p-2">{event.success ? '成功' : `失败 · ${event.reason}`}</td><td className="max-w-xs truncate p-2" title={event.user_agent}>{event.user_agent}</td></tr>)}</tbody></table>{events.data?.total === 0 && <p className="p-4 text-xs text-muted">暂无登录记录</p>}</div>}
    <div className="flex gap-3 text-xs text-secondary"><button disabled={eventOffset === 0} onClick={() => setEventOffset(Math.max(0, eventOffset - 50))}>上一页</button><span>共 {events.data?.total ?? 0} 条</span><button disabled={eventOffset + 50 >= (events.data?.total ?? 0)} onClick={() => setEventOffset(eventOffset + 50)}>下一页</button></div>
    {confirmation && <Modal ariaLabel={confirmation.action === 'disable' ? '确认禁用用户' : '确认强制下线'} onClose={() => { if (!change.isPending) setConfirmation(null) }}>
      <div className="p-5 space-y-4">
        <h3 className="font-semibold text-foreground">{confirmation.action === 'disable' ? '确认禁用用户' : '确认强制下线'}</h3>
        <p className="text-sm text-secondary break-words">{confirmation.action === 'disable'
          ? `确定禁用用户「${confirmation.user.username}」吗？禁用后该用户将立即下线，且无法登录，直到恢复账号。`
          : `确定将用户「${confirmation.user.username}」强制下线吗？该用户的当前会话将失效，需要重新登录。`}</p>
        {change.error && <p role="alert" className="text-xs text-danger">{change.error.message}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" disabled={change.isPending} onClick={() => setConfirmation(null)} className="rounded-btn px-4 py-2 text-sm text-secondary hover:bg-elevated disabled:opacity-50">取消</button>
          <button type="button" disabled={change.isPending} onClick={() => {
            if (change.isPending) return
            change.mutate(() => confirmation.action === 'disable'
              ? api.updateUser(confirmation.user.id, { is_active: false })
              : api.revokeUserSession(confirmation.user.id), {
              onSuccess: () => {
                if (confirmation.action === 'revoke') toast(`已将用户「${confirmation.user.username}」强制下线`, 'success')
              },
            })
          }} className="rounded-btn bg-danger px-4 py-2 text-sm text-white disabled:opacity-50">{change.isPending ? '处理中…' : confirmation.action === 'disable' ? '确认禁用' : '确认下线'}</button>
        </div>
      </div>
    </Modal>}
    {(editing || resetting) && <Modal ariaLabel={resetting ? '重置临时密码' : editing === 'new' ? '创建用户' : '编辑用户'} onClose={() => { if (!change.isPending) { setEditing(null); setResetting(null); setPassword('') } }}><form className="p-5 space-y-4" onSubmit={e => { e.preventDefault(); if (resetting) change.mutate(() => api.resetUserPassword(resetting.id, password)); else if (editing === 'new') change.mutate(() => api.createUser({ username, password, role, pro_expires_on: role === 'pro' ? expires : null })); else if (editing) change.mutate(() => api.updateUser(editing.id, { role, pro_expires_on: role === 'pro' ? expires : null })) }}>
      <h3 className="font-semibold text-foreground">{resetting ? `重置 ${resetting.username} 的临时密码` : editing === 'new' ? '创建用户' : `编辑 ${editing?.username}`}</h3>
      {editing === 'new' && <label className="block text-xs text-secondary">用户名<input aria-label="用户名" required minLength={3} maxLength={64} pattern="[A-Za-z0-9_.-]+" className={`${input} mt-1`} value={username} onChange={e => setUsername(e.target.value)} /></label>}
      {(editing === 'new' || resetting) && <label className="block text-xs text-secondary">临时密码<input aria-label="临时密码" required minLength={8} maxLength={128} type="password" autoComplete="new-password" className={`${input} mt-1`} value={password} onChange={e => setPassword(e.target.value)} /></label>}
      {!resetting && <><label className="block text-xs text-secondary">角色<select aria-label="角色" className={`${input} mt-1`} value={role} onChange={e => { const next = e.target.value as UserRole; setRole(next); if (editing === 'new' && next === 'pro' && !expires) setExpires(defaultProExpiry()) }}><option value="guest">Guest</option><option value="pro">Pro</option></select></label>{role === 'pro' && <label className="block text-xs text-secondary">Pro 到期日（北京时间）<input aria-label="Pro 到期日" type="date" required className={`${input} mt-1`} value={expires} onChange={e => setExpires(e.target.value)} /></label>}</>}
      {change.error && <p role="alert" className="text-xs text-danger">{change.error.message}</p>}
      <button disabled={change.isPending} className="rounded-btn bg-accent px-4 py-2 text-sm text-white disabled:opacity-50">{change.isPending ? '保存中…' : '保存'}</button>
    </form></Modal>}
  </section>
}
