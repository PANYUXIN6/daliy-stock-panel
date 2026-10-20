import type { AiCredits, User } from '@/lib/api'

export const guestUser: User = {
  id: 'guest-test', username: 'guest', role: 'guest', is_initial_admin: false,
  pro_expires_on: null, is_active: true, must_change_password: false,
  created_at: '', last_login_at: null, last_login_ip: null, login_count: 1,
}

export function creditsFixture(overrides: Partial<AiCredits> = {}): AiCredits {
  return {
    policy_version: 7, period: '2026-10', resets_at: '2026-11-01T00:00:00+08:00',
    role: 'guest', unlimited: false, monthly_points: 60, used_points: 0,
    reserved_points: 0, remaining_points: 60, cost_limit_cny: null, cost_limits_enabled: false, cost_used_cny: '0',
    features: {
      assistant: { points: 3, available: true, reason: null },
      stock_analysis: { points: 5, available: true, reason: null },
      financial_analysis: { points: 5, available: true, reason: null },
      market_recap: { points: 5, available: true, reason: null },
      concept_rotation: { points: 5, available: true, reason: null },
      industry_rotation: { points: 5, available: true, reason: null },
    }, ...overrides,
  }
}
