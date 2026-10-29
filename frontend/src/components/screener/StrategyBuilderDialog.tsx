import { Modal } from '@/components/Modal'

interface Props {
  open: boolean
  onClose: () => void
  onSavedId?: (id: string, researchOnly?: boolean) => void | Promise<void>
  mode?: 'create' | 'modify'
  existingStrategyIds?: ReadonlySet<string>
}

export function StrategyBuilderDialog(props: Props) {
  if (!props.open) return null
  return <Modal onClose={props.onClose} ariaLabel="策略代码仅服务器部署"><div className="p-6 text-sm text-secondary">网页不支持编写、导入或 AI 生成 Python 策略。请选择可信策略模板、配置个人参数或使用声明式公式。<button className="mt-4 block text-accent" onClick={props.onClose}>关闭</button></div></Modal>
}
