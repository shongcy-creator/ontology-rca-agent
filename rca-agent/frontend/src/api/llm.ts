/**
 * LLM 运行时配置 API。
 *
 * 边界（与后端一致，界面必须如实告知使用人）：
 *  · 切换**只对当前后端进程生效**，重启后回到环境变量/compose 的配置；
 *  · API Key 只存在后端内存里、不落盘；读取时一律脱敏；
 *  · provider 必须在后端注册表内（界面不提供任意 base_url 自由填写，
 *    避免把服务端变成向任意地址发请求的通道）。
 */

const BASE = '/api/agent'

export interface ProviderInfo {
  api: string
  base_url: string
  default_model: string
  fallback_model: string
  key_present: boolean
  key_masked: string
}

export interface RuntimeOverride {
  provider: string | null
  model: string | null
  base_url: string | null
  fallback_model: string | null
  key_providers: string[]
  active: boolean
}

export interface LLMConfig {
  provider: string
  api: string
  base_url: string
  model: string
  fallback_model: string
  api_key: string
  api_key_present?: boolean
  providers: Record<string, ProviderInfo>
  runtime: RuntimeOverride
  budgets?: Record<string, number>
}

export interface LLMTestResult {
  ok: boolean
  provider?: string
  requested_model?: string
  model?: string
  /** 实际模型与请求模型不一致（说明发生了降级） */
  model_mismatch?: boolean
  base_url?: string
  latency_ms?: number
  tokens?: number
  sample?: string
  error?: string
}

export interface LLMSwitchResult {
  ok: boolean
  changed?: Record<string, unknown>
  provider?: string
  model?: string
  base_url?: string
  api_key?: string
  need_api_key?: boolean
  runtime?: RuntimeOverride
  note?: string
  error?: string
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  const text = await res.text()
  let body: unknown = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = { error: text }
  }
  if (!res.ok) {
    const b = body as Record<string, unknown>
    const err = new Error(String(b?.error ?? b?.detail ?? `HTTP ${res.status}`)) as
      Error & { status?: number; body?: unknown }
    err.status = res.status
    err.body = body
    throw err
  }
  return body as T
}

export const llmApi = {
  config: () => req<LLMConfig>('/config'),
  /** 保存并切换（运行中生效） */
  switchTo: (p: {
    provider?: string
    model?: string
    base_url?: string
    api_key?: string
    fallback_model?: string
  }) => req<LLMSwitchResult>('/llm', { method: 'POST', body: JSON.stringify(p) }),
  /** 先测后切：只试一次真实调用，不改配置 */
  test: (p: {
    provider?: string
    model?: string
    base_url?: string
    api_key?: string
  }) => req<LLMTestResult>('/llm/test', { method: 'POST', body: JSON.stringify(p) }),
  reset: () => req<LLMSwitchResult>('/llm/reset', { method: 'POST' }),
}
