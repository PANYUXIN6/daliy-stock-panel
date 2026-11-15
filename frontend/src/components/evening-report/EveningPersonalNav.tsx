import { NavLink } from 'react-router-dom'

export function EveningPersonalNav() {
  return <nav aria-label="晚报与个人记录" className="flex flex-wrap gap-1 border-b border-border pb-2 text-sm">
    {[['/evening-report', '每日晚报'], ['/evening-report/history', '观察历史'], ['/evening-report/overview', '反馈概览']].map(([to, name]) => <NavLink key={to} to={to} end className={({ isActive }) => `rounded-md px-4 py-2 font-medium focus-visible:outline-accent ${isActive ? 'bg-accent/10 text-accent' : 'text-secondary hover:bg-elevated hover:text-foreground'}`}>{name}</NavLink>)}
  </nav>
}
