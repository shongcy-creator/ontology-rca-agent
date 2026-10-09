import { create } from 'zustand'
import type { RCACandidate } from './chatStore'

// ── 类型 ──────────────────────────────────────────────────────────────────

export interface AgentEvent {
  event: string          // route | run_start | step_start | reasoning | tool_call | observation | step_end | final | error
  data: Record<string, unknown>
}

export interface AgentStepView {
  step: number
  phase: 'observe' | 'reason' | 'act' | ''
  reasoning?: string
  tool_name?: string
  tool_args?: Record<string, unknown>
  observation?: string
  ok?: boolean
  latency_ms?: number
}

export interface AgentFinal {
  run_id: string
  incident_id: string
  mode: string
  status: string
  // 复用 chatStore 的 RCACandidate：结论的"可解释字段"（机制/归因/判据/关键词）
  // 必须与 RCA 面板共享同一份类型，否则两处渲染会漂移。
  root_cause: RCACandidate | null
  confidence: number
  evidence: Array<{ source?: string; finding?: string; value?: unknown }>
  reasoning_summary: string
  next_actions: Array<{ urgency?: string; action?: string; needs_approval?: boolean }>
  steps_used: number
  total_tokens: number
  latency_ms: number
  seed_agreement: boolean | null
  seed_agreement_level: string | null
  tools_used: string[]
  /** 运行失败/回退时的原因（后端 AgentResult.error） */
  error?: string
  /** 后端随 final 一起下发的推理步骤（快路径不发 step_* 事件，只能从这里取） */
  steps?: unknown[]
  route?: { mode: string; reason: string }
  seed?: Record<string, unknown>
  topology?: unknown[]
  topology_edges?: unknown[]
  root_cause_path?: unknown[]
}

// ── Store ──────────────────────────────────────────────────────────────────

/**
 * 把后端 `AgentStep.to_dict()` 的原始结构归一化成前端视图结构。
 *
 * 两边字段名不同：后端 `tool_input` / `observation_ok`，前端 `tool_args` / `ok`。
 * 为什么需要它：**确定性快路径不发 `step_*` SSE 事件**（它不做 observe/act 循环），
 * 推理步骤只随 `final` 一起下发 —— 只读 store 里的 `steps` 会得到空数组，
 * 于是"推理过程"在聊天框里永远是空的。
 */
export function normalizeSteps(raw: unknown): AgentStepView[] {
  if (!Array.isArray(raw)) return []
  return raw.map((x) => {
    const s = (x ?? {}) as Record<string, unknown>
    return {
      step: Number(s.step ?? s.step_no ?? 0),
      phase: String(s.phase ?? '') as AgentStepView['phase'],
      reasoning: String(s.reasoning ?? ''),
      tool_name: String(s.tool_name ?? ''),
      tool_args: (s.tool_args ?? s.tool_input ?? {}) as Record<string, unknown>,
      observation: String(s.observation ?? ''),
      ok: (s.ok ?? s.observation_ok) as boolean | undefined,
      latency_ms: Number(s.latency_ms ?? 0),
    }
  })
}

interface AgentStore {
  streaming: boolean
  events: AgentEvent[]
  steps: AgentStepView[]
  currentStep: number
  routeInfo: { mode: string; reason: string } | null
  final: AgentFinal | null
  error: string | null

  sendDiagnose: (text: string, mode?: 'deterministic' | 'agentic') => Promise<void>
  clear: () => void
}

const API = '/api'

export const useAgentStore = create<AgentStore>((set, get) => ({
  streaming: false,
  events: [],
  steps: [],
  currentStep: 0,
  routeInfo: null,
  final: null,
  error: null,

  sendDiagnose: async (text, mode) => {
    set({
      streaming: true, events: [], steps: [], currentStep: 0,
      routeInfo: null, final: null, error: null,
    })

    try {
      const resp = await fetch(`${API}/agent/diagnose/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ alert: text, severity: 'P1', mode }),
      })

      if (!resp.ok || !resp.body) {
        throw new Error(`HTTP ${resp.status}: ${await resp.text()}`)
      }

      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      const handleEvent = (eventName: string, payload: Record<string, unknown>) => {
        set(s => ({ events: [...s.events, { event: eventName, data: payload }] }))

        switch (eventName) {
          case 'route':
            set({ routeInfo: { mode: String(payload.mode ?? ''), reason: String(payload.reason ?? '') } })
            break

          case 'step_start':
            set({ currentStep: Number(payload.step ?? 0) })
            break

          case 'reasoning':
            set(s => {
              const step = Number(payload.step ?? s.currentStep)
              const existing = s.steps.find(x => x.step === step && x.phase === 'reason')
              if (existing) {
                return {
                  steps: s.steps.map(x => x === existing
                    ? { ...x, reasoning: String(payload.reasoning ?? x.reasoning ?? '') }
                    : x),
                }
              }
              return {
                steps: [...s.steps, {
                  step, phase: 'reason',
                  reasoning: String(payload.reasoning ?? ''),
                }],
              }
            })
            break

          case 'tool_call':
            set(s => {
              const step = Number(payload.step ?? s.currentStep)
              return {
                steps: [...s.steps, {
                  step, phase: 'act',
                  tool_name: String(payload.name ?? ''),
                  tool_args: (payload.args as Record<string, unknown>) ?? {},
                }],
              }
            })
            break

          case 'observation':
            set(s => {
              // 把观察结果挂到最近一个同名 act 步骤
              const name = String(payload.name ?? '')
              const idx = [...s.steps].map((x, i) => ({ x, i }))
                .reverse().find(({ x }) => x.phase === 'act' && x.tool_name === name)
              if (idx) {
                return {
                  steps: s.steps.map((x, i) => i === idx.i
                    ? {
                        ...x,
                        observation: String(payload.summary ?? ''),
                        ok: Boolean(payload.ok),
                        latency_ms: Number(payload.latency_ms ?? 0),
                      }
                    : x),
                }
              }
              return s
            })
            break

          case 'final':
            set({ final: payload as unknown as AgentFinal, streaming: false })
            break

          case 'error':
            set({ error: String((payload.error as string) ?? '未知错误'), streaming: false })
            break
        }
      }

      // 逐块读取并解析 SSE
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // 按空行切分 SSE 事件
        const chunks = buffer.split('\n\n')
        buffer = chunks.pop() ?? ''

        for (const chunk of chunks) {
          let eventName = 'message'
          const dataLines: string[] = []
          for (const line of chunk.split('\n')) {
            if (line.startsWith('event: ')) {
              eventName = line.slice(7).trim()
            } else if (line.startsWith('data: ')) {
              dataLines.push(line.slice(6))
            }
          }
          if (dataLines.length === 0) continue
          try {
            const payload = JSON.parse(dataLines.join('\n'))
            handleEvent(eventName, payload)
          } catch {
            /* 忽略解析失败的心跳/注释 */
          }
        }
      }
    } catch (err) {
      set({ error: String(err), streaming: false })
    }
  },

  clear: () => set({
    streaming: false, events: [], steps: [], currentStep: 0,
    routeInfo: null, final: null, error: null,
  }),
}))
