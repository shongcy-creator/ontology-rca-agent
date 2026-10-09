/**
 * 故障注入控制台 API 客户端。
 *
 * 与 Agent 的边界：这里的接口只"出题"（制造故障），不"答题"。
 * 场景里带的 `expected_root_cause` 是给**人工对照**用的标准答案，
 * 页面会明确标注"不会发送给智能体"，避免破坏"智能体不知道答案"的前提。
 */

const BASE = '/api/chaos'

/** 需要显式确认才允许注入的场景（影响面大） */
export type Risk = 'low' | 'medium' | 'high' | 'critical'

export interface ParamSpec {
  name: string
  default: number | string | null
  help: string
  type: 'int' | 'str'
}

export interface Scenario {
  id: string
  layer: string
  layer_label: string
  title: string
  description: string
  category: string
  signals: string[]
  recovered_signals: string[]
  default_params: Record<string, number | string>
  params_help: Record<string, string>
  params_spec: ParamSpec[]
  selector: string
  expected_root_cause: string
  ontology_terms: string[]
  needs_stress: boolean
  signal_needs_stress: boolean
  alert_names: string[]
  blast_radius: string
  risk: Risk
  impact: string
}

export interface EngineHealth {
  available: boolean
  enabled: boolean
  reason: string
  scenarios?: number
  state_file?: string
  state_file_exists?: boolean
  prometheus?: string
  app_lb?: string
  docker_cli?: string
  token_required?: boolean
}

export interface SignalState {
  state: 'active' | 'quiet' | 'quiet_under_load' | 'unknown'
  reason: string
}

export interface ActiveFault {
  fault_id: string
  layer?: string
  category?: string
  title?: string
  targets?: string[]
  params?: Record<string, unknown>
  injected_at?: string
  expected_root_cause?: string
  /** 该记录对应故障的信号当前是否还成立（用于识别"名存实亡"的残留记录） */
  signal_state?: SignalState
}

export interface ContainerInfo {
  id?: string
  name: string
  state: string
  status: string
  memory_limit_mb?: number
}

export interface ClusterStatus {
  active_faults: ActiveFault[]
  state_file: string
  now: string
  app_replicas: ContainerInfo[]
  mysql: Record<string, Record<string, string | null>>
  gateway: ContainerInfo | null
  prometheus_up: { up: number; total: number } | null
  engine: EngineHealth
  layers: Record<string, number>
  /** 有几条激活记录"名存实亡"（信号全部不成立） */
  stale_count?: number
  [k: string]: unknown
}

export interface CheckItem {
  name: string
  ok: boolean
  detail: unknown
}

export interface DoctorResult {
  passed: boolean
  ok_count: number
  total: number
  checks: CheckItem[]
}

export interface SignalRow {
  kind: 'inject' | 'recover'
  expr: string
  value: number | null
  truthy: boolean
  error?: string
}

export interface SignalSnapshot {
  ok: boolean
  fault_id: string
  signals: SignalRow[]
}

export interface JobLog {
  t: number
  level: 'info' | 'warn' | 'error' | 'success'
  msg: string
}

export interface Job {
  id: string
  kind: string
  fault_id: string
  params: Record<string, unknown>
  targets: string[]
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
  /** 是否已收到停止请求（协作式取消：下一个取消点停下并回滚） */
  cancel_requested?: boolean
  step: string
  started_at: number
  finished_at: number | null
  duration_s: number
  result: Record<string, unknown>
  error: string
  log_count: number
  logs?: JobLog[]
  log_offset?: number
}

/**
 * 故障注入接口的访问令牌（对应后端 `CHAOS_API_TOKEN`）。
 *
 * 为什么存在：这些接口等价于"能操作这些容器"，对外暴露前必须设令牌
 * （见 docker-compose.yml 顶部的安全默认值说明）。令牌只存在浏览器本地，
 * 不会随诊断请求发给 Agent。后端未设 `CHAOS_API_TOKEN` 时，这里为空即可。
 */
const TOKEN_KEY = 'cc-ops:chaos-token'

export function getChaosToken(): string {
  try { return localStorage.getItem(TOKEN_KEY) || '' } catch { return '' }
}

export function setChaosToken(tok: string): void {
  try {
    if (tok) localStorage.setItem(TOKEN_KEY, tok)
    else localStorage.removeItem(TOKEN_KEY)
  } catch { /* 隐私模式下忽略 */ }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const tok = getChaosToken()
  const res = await fetch(BASE + path, {
    headers: {
      'Content-Type': 'application/json',
      ...(tok ? { 'X-Chaos-Token': tok } : {}),
    },
    ...init,
  })
  const text = await res.text()
  let body: unknown = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = { detail: text }
  }
  if (!res.ok) {
    const b = body as Record<string, unknown>
    const err = new Error(
      String(b?.error ?? b?.detail ?? `HTTP ${res.status}`)
    ) as Error & { status?: number; body?: unknown }
    err.status = res.status
    err.body = body
    throw err
  }
  return body as T
}

export const chaosApi = {
  health: () => req<EngineHealth>('/health'),

  scenarios: () =>
    req<{ total: number; by_layer: Record<string, number>; layer_labels: Record<string, string>; scenarios: Scenario[] }>(
      '/scenarios'
    ),

  status: () => req<ClusterStatus>('/status'),

  doctor: () => req<DoctorResult>('/doctor'),

  preview: (fault_id: string) =>
    req<{ ok: boolean; selector: string; targets: string[]; count: number; error?: string }>(
      '/preview',
      { method: 'POST', body: JSON.stringify({ fault_id }) }
    ),

  signals: (fault_id: string) => req<SignalSnapshot>(`/signals/${fault_id}`),

  /**
   * 人工注入。高危险场景需要 confirm=true（后端会先回 409 要求确认）。
   * `targets` 传入预览到的目标 → 后端会用它而不是重新解析 selector，
   * 保证「预览即实际目标」（roundrobin 场景尤其重要）。
   * 返回 job_id，调用方负责轮询日志。
   */
  inject: (
    fault_id: string,
    params: Record<string, unknown>,
    confirm: boolean,
    targets: string[] = []
  ) =>
    req<{ ok: boolean; job_id?: string; job?: Job; error?: string; need_confirm?: boolean; risk?: Risk; impact?: string }>(
      '/inject',
      { method: 'POST', body: JSON.stringify({ fault_id, params, confirm, targets }) }
    ),

  recover: (fault_id?: string, all?: boolean) =>
    req<{ ok: boolean; job_id?: string; job?: Job; error?: string; busy?: boolean }>('/recover', {
      method: 'POST',
      body: JSON.stringify(all ? { all: true } : { fault_id }),
    }),

  cleanup: () =>
    req<{ ok: boolean; job_id?: string; job?: Job; error?: string; busy?: boolean }>('/cleanup', {
      method: 'POST',
    }),

  verify: (payload: { fault_ids?: string[]; hold?: number; stress?: string; full?: boolean }) =>
    req<{ ok: boolean; job_id?: string; job?: Job; error?: string; busy?: boolean }>('/verify', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  jobs: (limit = 30) => req<{ busy: boolean; jobs: Job[] }>(`/jobs?limit=${limit}`),

  job: (id: string, since = 0) => req<Job>(`/jobs/${id}?since=${since}`),

  cancel: () =>
    req<{ ok: boolean; error?: string; note?: string; already_requested?: boolean; job?: Job }>(
      '/jobs/current/cancel',
      { method: 'POST' }
    ),

  /** 一键「停止并强制回滚」：请求取消 + 排队兜底清理（误点演练的紧急出口） */
  stopAndRollback: () =>
    req<{ ok: boolean; cancel_requested: boolean; cleanup_job: string | null; cleanup_busy: boolean; note: string }>(
      '/stop_and_rollback',
      { method: 'POST' }
    ),
}

/** 把一次注入的结果整理成"给智能体看的中性告警文本"（不泄露标准答案）。 */
export function neutralPrompt(sc: Scenario, targets: string[]): string {
  const tgt = targets.length ? targets.join('、') : 'payment-app 集群'
  return `${tgt} 出现异常，请诊断根因。告警来源：${sc.alert_names.join('、') || '集群监控'}（layer=${sc.layer}，场景=${sc.title}）`
}

// ── 压测台 ─────────────────────────────────────────────────────────────────

export interface StressTarget {
  id: string
  label: string
  url: string
  kind: 'http' | 'mysql'
}

export interface StressLimits {
  [k: string]: { min: number; max: number; default: number }
}

export interface StressPreset {
  id: string
  label: string
  params: Record<string, unknown>
  confirm?: boolean
}

export interface StressParams {
  mode: 'http' | 'mysql' | 'mixed'
  target: string
  path: string
  method: string
  body: string
  concurrency: number
  duration: number
  requests: number
  timeout: number
  ramp_up: number
  label: string
  mysql_query: string
  mysql_hold_connections: number
}

export interface StressPoint {
  t: number
  count: number
  errors: number
  p99_ms: number
}

export interface StressSummary {
  source?: string
  label?: string
  mode?: string
  started_at?: string
  wall_seconds?: number
  concurrency?: number
  target?: { url?: string; path?: string; method?: string; mysql?: string; mysql_query?: string }
  requests?: { total: number; success: number; error: number; success_rate: number }
  latency_ms?: Record<string, number>
  throughput?: { requests_per_sec: number; bytes_per_sec: number; bytes_total: number }
  status_codes?: Record<string, number>
  error_kinds?: Record<string, number>
  time_series?: StressPoint[]
}

export interface StressOptions {
  modes: Array<{ id: string; label: string }>
  targets: StressTarget[]
  limits: StressLimits
  defaults: StressParams
  presets: StressPreset[]
  history: StressSummary[]
}

export interface StressRunResult {
  ok: boolean
  need_confirm?: boolean
  impact?: string
  error?: string
  job_id?: string
}

export const stressApi = {
  options: () => req<StressOptions>('/stress/options'),
  history: (limit = 20) => req<{ history: StressSummary[] }>(`/stress/history?limit=${limit}`),
  run: (params: Partial<StressParams>, confirm = false) =>
    req<StressRunResult>('/stress/run', {
      method: 'POST',
      body: JSON.stringify({ ...params, confirm }),
    }),
}
