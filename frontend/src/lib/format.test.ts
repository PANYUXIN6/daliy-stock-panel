// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { cnDateTimeFromUtc, fmtBigNum } from './format'

describe('fmtBigNum: 大数单位显示', () => {
  it('现金流金额保留负号并换算为亿', () => {
    expect(fmtBigNum(-37_944_297_802)).toBe('-379.44亿')
    expect(fmtBigNum(70_691_364_610)).toBe('706.91亿')
  })

  it.each([
    [9_999, '9999'],
    [10_000, '1万'],
    [99_999_999, '10000万'],
    [100_000_000, '1.00亿'],
    [999_999_999_999, '10000.00亿'],
    [1_000_000_000_000, '1.00万亿'],
  ] as const)('正负 %s 使用相同单位与精度', (value, expected) => {
    expect(fmtBigNum(value)).toBe(expected)
    expect(fmtBigNum(-value)).toBe(`-${expected}`)
  })

  it('零保持为零，空值与 NaN 保持为占位符', () => {
    expect(fmtBigNum(0)).toBe('0')
    expect(fmtBigNum(null)).toBe('—')
    expect(fmtBigNum(undefined)).toBe('—')
    expect(fmtBigNum(Number.NaN)).toBe('—')
  })
})

// 后端自选 added_at 是 datetime.utcnow() 写入的 naive UTC 串 (无 Z 后缀)。
// 若按 JS 默认的本地时区解析, 非北京时区会显示错日; 北京时间 00:00-08:00 加入的
// 记录则会被显示成前一天。以下用例在任何时区的机器上都必须通过。
describe('cnDateTimeFromUtc: naive UTC 串 → 北京时间', () => {
  it('跨 UTC 日界: 北京凌晨加入的不能显示成前一天', () => {
    // UTC 09-18 20:30 = 北京 09-19 04:30
    expect(cnDateTimeFromUtc('2026-09-18T20:30:00')).toBe('2026-09-19 04:30:00')
  })

  it('常规时段换算 (+8 小时)', () => {
    expect(cnDateTimeFromUtc('2026-09-19T03:21:07')).toBe('2026-09-19 11:21:07')
  })

  it('北京午夜显示 00:00 而非 24:00', () => {
    expect(cnDateTimeFromUtc('2026-09-18T16:00:00')).toBe('2026-09-19 00:00:00')
  })

  it('已带时区标记的串按原语义解析', () => {
    expect(cnDateTimeFromUtc('2026-09-18T20:30:00Z')).toBe('2026-09-19 04:30:00')
    expect(cnDateTimeFromUtc('2026-09-19T04:30:00+08:00')).toBe('2026-09-19 04:30:00')
  })

  it('空值与非法值返回空串 (不抛异常、不产生 Invalid Date)', () => {
    for (const v of ['', '   ', null, undefined, 'garbage', 'not-a-date']) {
      expect(cnDateTimeFromUtc(v)).toBe('')
    }
  })
})
