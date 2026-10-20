// 与浏览器标签页共用图标文件，保持各入口的品牌图标一致。
interface LogoProps {
  className?: string
  size?: number
  style?: React.CSSProperties
}

export function Logo({ className, size = 32, style }: LogoProps) {
  return (
    <img
      src="/favicon.svg"
      alt="小帅工作台"
      width={size}
      height={size}
      className={className}
      style={style}
    />
  )
}
