/**
 * 成本 / 限流 / 演化就绪度 API。
 *
 * 这三个端点后端早已就绪（`GET /api/agent/cost`、`/rate-limit`、`/evolution`），
 * 缺的只是把它们呈现出来的页面 —— 因此本文件不做任何计算，
 * 只把服务端给的数字原样取回（避免"前端自己算一套、和后端不一致"）。
 */

const BASE = '/api/agent'

export interface CostStore {
  backend: string
  total_runs: number
  total_tokens: number
  avg_latency_ms: number
  avg_confidence: number
  by_mode: Record<string, number>
  by_status: Record<string, number>
  total_thoughts: number
}

export interface Pricing {
  input: number
  output: number
}

export interface CostReport {
  store: CostStore
  total_tokens: number
  estimated_cost_usd: number
  pricing_per_1m_tokens: Record<string, Pricing>
}

export interface RateLimit {
  max_per_minute: number
  current_in_window: number
  remaining: number
}

export interface EvolutionReport {
  new_trajectories: number
  threshold_trajectories: number
  trigger_by_trajectories: boolean
  last_evolution_ts: string | null
  days_since_last: number | null
  threshold_days: number
  trigger_by_days: boolean
  ready: boolean
  evoontology?: {
    status: string
    state?: Record<string, unknown>
    check?: Record<string, unknown>
  }
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(BASE + path)
  const text = await res.text()
  let body: unknown = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = { error: text }
  }
  if (!res.ok) {
    const b = body as Record<string, unknown>
    throw new Error(String(b?.error ?? b?.detail ?? `HTTP ${res.status}`))
  }
  return body as T
}

export const costApi = {
  cost: () => get<CostReport>('/cost'),
  rateLimit: () => get<RateLimit>('/rate-limit'),
  evolution: () => get<EvolutionReport>('/evolution'),
}
