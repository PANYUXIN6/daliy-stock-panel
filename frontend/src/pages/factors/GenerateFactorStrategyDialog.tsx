import { Modal } from '@/components/Modal'

export function GenerateFactorStrategyDialog({ onClose }: {
  item: { id: string; label: string; group: string; direction?: 'high' | 'low' | 'none' }
  onClose: () => void
}) {
  return <Modal onClose={onClose} ariaLabel="因子策略模板"><div className="p-5 text-sm text-secondary">因子 Python 策略需由服务器维护者部署。仍可使用因子公式、条件信号和已有策略参数。<button className="mt-4 block text-accent" onClick={onClose}>关闭</button></div></Modal>
}
