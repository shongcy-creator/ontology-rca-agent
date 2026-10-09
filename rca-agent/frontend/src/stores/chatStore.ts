import { create } from 'zustand'

// ── Types ────────────────────────────────────────────────────────────────────

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  tool_calls?: ToolCall[]
  timestamp?: number
  incident_id?: string
  /** 本轮的推理快照（把推理过程留在对话里，回看时仍可展开） */
  trace?: import('./agentStore').AgentStepView[]
  /** 本轮的路由判定（确定性快路径 / LLM Agent 及其理由） */
  route?: { mode: string; reason: string }
}

export interface ToolCall {
  id: string
  name: string
  arguments: Record<string, unknown>
}

export interface TopologyNode {
  id: string
  name: string
  type: string
  scope?: string
  relation_type?: string
  definition?: string
}

export interface TopologyEdge {
  id: string
  source: string
  target: string
  relation_type: string
  condition?: string
}

export interface PathHop {
  from: string
  to: string
  relation: string
  relation_type: string
  condition?: string
}

/** 本体里的一个"关联对象"（归因对象 / 观测佐证） */
export interface OntoRef {
  id: string
  label?: string
  condition?: string
  description?: string
}

/** 以该根因术语为 target 的本体约束（判据口径） */
export interface OntoConstraint {
  id: string
  description?: string
  severity?: string
  scope?: string
  constraint_type?: string
  trigger_keywords?: string[]
}

export interface RCACandidate {
  category: string
  entity_id: string
  entity_name?: string
  confidence: number
  reason?: string
  /** ── 以下为「让结论能自解释」的本体解释性字段 ── */
  /** 机制：这个根因在技术上是怎么回事 */
  definition?: string
  /** 本体给出的判别理由 */
  rationale?: string
  role?: string
  lifecycle?: string
  scope?: string
  /** 命中的打分关键词（判别依据） */
  matched_keywords?: string[]
  /** 归因对象：根因实际落在哪个实例/组件上 */
  affected?: OntoRef[]
  /** 观测佐证：哪条指标/事件在印证 */
  evidenced_by?: OntoRef[]
  /** 判据：约束给出的阈值/容量口径 */
  constraints?: OntoConstraint[]
  /** LLM Agent 路径下的自由文本描述 */
  description?: string
}

export interface RCAChainStep {
  step: number
  type: string
  description?: string
  keywords?: string[]
  source?: string
}

/** 一条处置动作（与后端 next_actions 同构）。ontology_v4 起由本体 remediation 提供。 */
export interface NextAction {
  urgency: string
  action: string
  command?: string
  needs_approval?: boolean
  /** 出处：ontology = 来自该根因术语的本体知识；category = 按类别的兜底建议 */
  source?: string
}

export interface RCAResult {
  incident_id: string
  alert: {
    message: string
    severity: string
    keywords?: string[]
    matched_categories?: string[]
  }
  evidence: Record<string, unknown>
  thresholds_triggered: Array<{ rule: string; status: string; value: unknown }>
  topology: TopologyNode[]
  topology_edges: TopologyEdge[]
  root_cause_path: PathHop[]
  candidates: RCACandidate[]
  root_cause: RCACandidate | null
  rca_chain: RCAChainStep[]
  elapsed_ms: number
  confidence: number
  /** 处置建议（ontology_v4 起来自本体 remediation，兜底为按类别的通用建议） */
  next_actions?: NextAction[]
}

export interface IncidentSummary {
  incident_id: string
  message: string
  severity: string
  category: string
  entity_id: string
  confidence: number
  elapsed_ms: number
}

// ── Store ────────────────────────────────────────────────────────────────────

interface ChatStore {
  sessionId: string
  messages: ChatMessage[]
  rcaResult: RCAResult | null
  incidents: IncidentSummary[]
  isLoading: boolean
  error: string | null

  sendMessage: (text: string) => Promise<void>
  loadIncidents: () => Promise<void>
  selectIncident: (incidentId: string) => Promise<void>
  clearError: () => void
  reset: () => void
}

const SESSION_ID = `session_${Date.now()}`
const API_BASE = '/api'

export const useChatStore = create<ChatStore>((set, get) => ({
  sessionId: SESSION_ID,
  messages: [],
  rcaResult: null,
  incidents: [],
  isLoading: false,
  error: null,

  sendMessage: async (text: string) => {
    const userMsg: ChatMessage = { role: 'user', content: text, timestamp: Date.now() }
    set(s => ({ messages: [...s.messages, userMsg], isLoading: true, error: null }))

    try {
      // Build full conversation history for the backend
      const allMessages = [...get().messages, userMsg].map(m => ({
        role: m.role === 'tool' ? 'assistant' : m.role,
        content: m.content,
      }))

      const resp = await fetch(`${API_BASE}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: allMessages, session_id: get().sessionId }),
      })

      if (!resp.ok) {
        const detail = await resp.text()
        throw new Error(`HTTP ${resp.status}: ${detail.slice(0, 300)}`)
      }

      const data = await resp.json()
      const assistantMsg: ChatMessage = {
        role: 'assistant',
        content: data.message?.content ?? '',
        timestamp: Date.now(),
        incident_id: data.incident_id ?? undefined,
      }
      set(s => ({ messages: [...s.messages, assistantMsg], isLoading: false }))

      // If backend produced an incident, fetch its structured result
      if (data.incident_id) {
        await get().selectIncident(data.incident_id)
        await get().loadIncidents()
      }
    } catch (err) {
      set({ isLoading: false, error: String(err) })
    }
  },

  loadIncidents: async () => {
    try {
      const resp = await fetch(`${API_BASE}/rca/incidents`)
      if (!resp.ok) return
      const data = await resp.json()
      set({ incidents: data.incidents ?? [] })
    } catch {
      /* non-fatal */
    }
  },

  selectIncident: async (incidentId: string) => {
    try {
      const resp = await fetch(`${API_BASE}/rca/incidents/${encodeURIComponent(incidentId)}`)
      if (!resp.ok) return
      const data = await resp.json()
      if (data.result) set({ rcaResult: data.result as RCAResult })
    } catch {
      /* non-fatal */
    }
  },

  clearError: () => set({ error: null }),

  reset: () =>
    set({
      messages: [],
      rcaResult: null,
      incidents: [],
      isLoading: false,
      error: null,
    }),
}))
