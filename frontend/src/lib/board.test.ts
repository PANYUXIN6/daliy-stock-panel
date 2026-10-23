import { describe, expect, it } from 'vitest'
import { boardTag, getBoardType } from './board'
import { boardTag as tableBoardTag } from '@/components/stock-table/primitives'

describe('board classification', () => {
  it.each([
    ['300001.SZ', '创业板', '创'],
    ['301001.SZ', '创业板', '创'],
    ['302132.SZ', '创业板', '创'],
    ['688001.SH', '科创板', '科'],
    ['689009.SH', '科创板', '科'],
    ['920001.BJ', '北交所', '北'],
  ])('classifies %s consistently in filters and table badges', (symbol, board, tag) => {
    expect(getBoardType(symbol)).toBe(board)
    expect(boardTag(symbol)).toBe(tag)
    expect(tableBoardTag(symbol)?.label).toBe(tag)
  })

  it.each(['600000.SH', '000001.SZ', '510300.SH'])('does not add growth-board badges to %s', symbol => {
    expect(boardTag(symbol)).toBe('')
    expect(tableBoardTag(symbol)).toBeNull()
  })
})
