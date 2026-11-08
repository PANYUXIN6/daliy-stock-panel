// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { fmtAssetPrice, fmtPrice, fmtPct, roundPriceToTick } from './format'
import { getLimitPrices } from './intraday-chart'

describe('价格按资产类型保留交易精度', () => {
  it('ETF 保留三位, 股票和旧调用方仍保留两位', () => {
    expect(fmtAssetPrice(0.827, 'etf')).toBe('0.827')
    expect(fmtAssetPrice(1, 'etf')).toBe('1.000')
    expect(fmtAssetPrice(12.34, 'stock')).toBe('12.34')
    expect(fmtAssetPrice(12.34)).toBe('12.34')
    expect(fmtPrice(1.234)).toBe('1.23')
    expect(fmtPct(0.01234)).toBe('+1.23%')
  })

  it('缺失值不伪造价格, 二进制尾差与半个报价单位正确取整', () => {
    for (const value of [null, undefined, NaN, Infinity]) expect(fmtAssetPrice(value, 'etf')).toBe('—')
    expect(fmtAssetPrice(0.8270000000000001, 'etf')).toBe('0.827')
    expect(roundPriceToTick(1.005, 'stock')).toBe(1.01)
    expect(roundPriceToTick(0.8305, 'etf')).toBe(0.831)
    expect(fmtAssetPrice(-0.00001, 'etf')).toBe('0.000')
  })

  it('涨跌停兜底按 ETF 千分位取整, 保留指定幅度及权威价格', () => {
    expect(getLimitPrices(0.752, undefined, 'etf')).toMatchObject({ limitUp: 0.827, limitDown: 0.677 })
    expect(getLimitPrices(0.755, { rate: 0.1, limit_up: null, limit_down: null, source: 'rule' }, 'etf')).toMatchObject({ limitUp: 0.831, limitDown: 0.680 })
    expect(getLimitPrices(1.235, { rate: 0.2, limit_up: null, limit_down: null, source: 'rule' }, 'etf')).toMatchObject({ limitUp: 1.482, limitDown: 0.988 })
    expect(getLimitPrices(0.752, { rate: 0.1, limit_up: 0.828, limit_down: 0.678, source: 'instrument' }, 'etf')).toMatchObject({ limitUp: 0.828, limitDown: 0.678 })
    expect(getLimitPrices(10.01)).toMatchObject({ limitUp: 11.01, limitDown: 9.01 })
  })
})

// 后端自选 added_at 是 datetime.utcnow() 写入的 naive UTC 串 (无 Z 后缀)。
// 若按 JS 默认的本地时区解析, 非北京时区会显示错日; 北京时间 00:00-08:00 加入的
// 记录则会被显示成前一天。以下用例在任何时区的机器上都必须通过。
