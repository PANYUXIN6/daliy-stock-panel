import { useState, type FormEvent } from 'react'
import { api, type AuthStatus } from '@/lib/api'
import { Modal } from '@/components/Modal'
import { Logo } from '@/components/Logo'
import { announceSessionChange } from '@/lib/sessionRuntime'

export function Auth({ status, error, retry, onAuthenticated }: {
  status: AuthStatus | null; error: string; retry: () => void; onAuthenticated: (status: AuthStatus) => void
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [localError, setLocalError] = useState('')
  const [pending, setPending] = useState(false)
  const change = status?.authenticated && status.user?.must_change_password
  async function submit(event: FormEvent) {
    event.preventDefault()
    setLocalError('')
    if (change && newPassword !== confirm) { setLocalError('两次新密码不一致'); return }
    setPending(true)
    try {
      const next = change ? await api.authChangePassword(password, newPassword) : await api.authLogin(username.trim(), password)
      setPassword(''); setNewPassword(''); setConfirm('')
      announceSessionChange()
      onAuthenticated(next)
    } catch (err) { setLocalError(err instanceof Error ? err.message : '请求失败，请重试') }
    finally { setPending(false) }
  }
  const inputClass = 'mt-1 h-10 w-full rounded-btn border border-border bg-base px-3 text-sm text-foreground focus:outline-none focus:border-accent'
  return <div className="min-h-screen bg-base">
    <Modal onClose={() => {}} closeOnBackdrop={false} labelledBy="auth-title" overlayClassName="fixed inset-0 z-[9999] flex items-center justify-center bg-base p-4" panelClassName="w-full max-w-sm max-h-[90dvh] overflow-y-auto rounded-card border border-border bg-surface p-6 shadow-xl">
      <Logo size={32} className="mb-4 text-accent" />
      <h1 id="auth-title" className="text-lg font-semibold text-foreground">{change ? '设置个人密码' : '登录小帅工作台'}</h1>
      {!status ? <div className="mt-4 text-sm text-secondary">{error || '正在验证身份…'}{error && <button className="ml-2 text-accent" onClick={retry}>重试</button>}</div>
        : !status.configured ? <p className="mt-4 text-sm leading-6 text-secondary">服务器尚未初始化管理员账号，请联系部署管理员配置初始用户名与密码后重启服务。</p>
        : <form onSubmit={submit} className="mt-4 space-y-4">
          <p className="text-xs leading-5 text-muted">{change ? `${status.user?.username}，当前为临时密码。请先更改密码，再进入工作台。` : '使用管理员分配的账号登录。会话保持 7 天，同一账号的新登录会使旧会话失效。'}</p>
          {!change && <label className="block text-xs text-secondary">用户名<input aria-label="用户名" autoComplete="username" required value={username} onChange={e => setUsername(e.target.value)} className={inputClass} /></label>}
          <label className="block text-xs text-secondary">{change ? '当前临时密码' : '密码'}<input aria-label={change ? '当前临时密码' : '密码'} type="password" autoComplete="current-password" required value={password} onChange={e => setPassword(e.target.value)} className={inputClass} /></label>
          {change && <><label className="block text-xs text-secondary">新密码<input aria-label="新密码" type="password" autoComplete="new-password" required minLength={8} value={newPassword} onChange={e => setNewPassword(e.target.value)} className={inputClass} /></label><label className="block text-xs text-secondary">确认新密码<input aria-label="确认新密码" type="password" autoComplete="new-password" required value={confirm} onChange={e => setConfirm(e.target.value)} className={inputClass} /></label></>}
          {localError && <p role="alert" className="text-xs text-danger">{localError}</p>}
          <button disabled={pending} className="h-10 w-full rounded-btn bg-accent text-sm font-medium text-white disabled:opacity-50">{pending ? '处理中…' : change ? '更改密码并继续' : '登录'}</button>
          {change && <button type="button" className="w-full text-xs text-muted" onClick={async () => { await api.authLogout(); announceSessionChange(); window.location.reload() }}>退出当前账号</button>}
        </form>}
    </Modal>
  </div>
}
