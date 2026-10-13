import { useState, type FormEvent } from 'react'
import { api } from '@/lib/api'
import { useCurrentUser } from '@/lib/auth'
import { accountStorage, announceSessionChange } from '@/lib/sessionRuntime'
import { refreshAlertToastConfig } from '@/components/AlertToast'

export function SettingsAccountPanel() {
  const user = useCurrentUser()
  const [password, setPassword] = useState('')
  const [nextPassword, setNextPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [toast, setToast] = useState(() => accountStorage.getItem('alert_toast_enabled') !== '0')
  const [sound, setSound] = useState(() => accountStorage.getItem('alert_sound_enabled') !== '0')
  async function submit(event: FormEvent) {
    event.preventDefault(); setError('')
    if (nextPassword !== confirm) { setError('两次新密码不一致'); return }
    setPending(true)
    try { await api.authChangePassword(password, nextPassword); announceSessionChange(); window.location.reload() }
    catch (err) { setError(err instanceof Error ? err.message : '更改失败') }
    finally { setPending(false) }
  }
  const input = 'mt-1 w-full rounded-btn border border-border bg-base px-3 py-2 text-sm text-foreground'
  return <div className="space-y-6">
    <section className="rounded-card border border-border bg-surface p-5"><h2 className="font-semibold text-foreground">{user?.username}</h2><p className="mt-1 text-xs text-muted">角色：{user?.role}{user?.role === 'pro' && ` · 有效至 ${user.pro_expires_on}（北京时间）`}</p></section>
    <section className="rounded-card border border-border bg-surface p-5"><h3 className="mb-4 text-sm font-medium text-foreground">个人网页提醒</h3><div className="space-y-3 text-sm text-secondary"><label className="flex items-center gap-2"><input type="checkbox" checked={toast} onChange={e => { const value = e.target.checked; setToast(value); accountStorage.setItem('alert_toast_enabled', value ? '1' : '0'); refreshAlertToastConfig() }} />显示监控告警浮窗</label><label className="flex items-center gap-2"><input type="checkbox" checked={sound} onChange={e => { const value = e.target.checked; setSound(value); accountStorage.setItem('alert_sound_enabled', value ? '1' : '0') }} />播放网页提示音</label></div></section>
    <form onSubmit={submit} className="max-w-md space-y-4 rounded-card border border-border bg-surface p-5"><h3 className="text-sm font-medium text-foreground">更改密码</h3><label className="block text-xs text-secondary">当前密码<input type="password" required autoComplete="current-password" className={input} value={password} onChange={e => setPassword(e.target.value)} /></label><label className="block text-xs text-secondary">新密码<input type="password" required minLength={8} maxLength={128} autoComplete="new-password" className={input} value={nextPassword} onChange={e => setNextPassword(e.target.value)} /></label><label className="block text-xs text-secondary">确认新密码<input type="password" required autoComplete="new-password" className={input} value={confirm} onChange={e => setConfirm(e.target.value)} /></label>{error && <p role="alert" className="text-xs text-danger">{error}</p>}<button disabled={pending} className="rounded-btn bg-accent px-4 py-2 text-sm text-white disabled:opacity-50">{pending ? '保存中…' : '更改密码'}</button></form>
  </div>
}
