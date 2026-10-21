import type { CustomSeriesOption } from 'echarts'

export const BULL_BEAR_UP = '#FF0000'
export const BULL_BEAR_DOWN = '#08FF16'

/** 绘制后端提供的多空线：逐段红涨绿跌，平走为红，缺值不跨空档连接。 */
export function buildBullBearLineSeries(values: readonly (number | null | undefined)[]): CustomSeriesOption {
  const segments: number[][] = []
  for (let index = 1; index < values.length; index++) {
    const value = values[index]
    const previous = values[index - 1]
    if (value == null || previous == null || !Number.isFinite(value) || !Number.isFinite(previous)) continue
    segments.push([index - 1, previous, index, value, value < previous ? 1 : 0])
  }
  return {
    name: '多空线', type: 'custom', data: segments,
    dimensions: ['start', 'previous', 'end', 'value', 'falling'],
    encode: { x: [0, 2], y: [1, 3] },
    silent: true, animation: false, clip: true, z: 3,
    renderItem: (_params, api) => {
      const start = api.coord([api.value(0), api.value(1)])
      const end = api.coord([api.value(2), api.value(3)])
      return {
        type: 'line',
        shape: { x1: start[0], y1: start[1], x2: end[0], y2: end[1] },
        style: { stroke: api.value(4) ? BULL_BEAR_DOWN : BULL_BEAR_UP, lineWidth: 3 },
      }
    },
  }
}
