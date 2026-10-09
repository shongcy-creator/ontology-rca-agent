import { useState, useRef, useEffect } from 'react'
import { useChatStore } from '../stores/chatStore'
import { useAgentStore, normalizeSteps } from '../stores/agentStore'
import { useResizablePanel } from '../hooks/useResizablePanel'
import { ChatWindow, type LiveTurn } from '../components/ChatWindow'
import { RCAResultPanel } from '../components/RCAResultPanel'
import { TopologyGraph } from '../components/TopologyGraph'
import { MetricsPanel } from '../components/MetricsPanel'
import { IncidentList } from '../components/IncidentList'
import { LLMSettings } from '../components/LLMSettings'
import { llmApi, type LLMConfig } from '../api/llm'
import { conclusionMarkdown } from '../components/ReasoningInline'
import './AgentPage.css'

// 推理过程已移入聊天框（见 ReasoningInline），右侧只保留"结论/拓扑/指标/历史"
type RightTab = 'rca' | 'topology' | 'metrics' | 'history'

const SHORTCUTS = [
  'payment-app P99 延迟超过 500ms，MySQL 连接池耗尽',
  '容器 OOM 重启，内存使用率 98%',
  'MySQL 慢查询增多，数据库响应变慢',
  '5xx 错误率突增，请求大量失败',
]

const DETAIL_DEFAULT_WIDTH = 420

interface AgentPageProps {
  /**
   * 从「故障注入控制台」交接过来的提示词。
   * 刻意只用中性文案（症状 + 告警来源），不包含注入场景的标准答案 ——
   * 保证"智能体不知道答案"这一端到端验证前提不被破坏。
   */
  initialPrompt?: string
}

export function AgentPage({ initialPrompt }: AgentPageProps = {}) {
  const {
    sessionId, messages, rcaResult, incidents,
    isLoading, error, clearError, reset, sendMessage, loadIncidents,
  } = useChatStore()

  const {
    streaming: agentStreaming, final: agentFinal, error: agentError,
    steps: agentSteps, routeInfo: agentRoute,
    sendDiagnose, clear: clearAgent,
  } = useAgentStore()

  const [rightTab, setRightTab] = useState<RightTab>('rca')
  const [input, setInput] = useState('')
  const [liveTurn, setLiveTurn] = useState<LiveTurn | null>(null)
  // 当前 LLM 配置（头部常驻显示）+ 模型设置弹窗开关
  const [llmCfg, setLlmCfg] = useState<LLMConfig | null>(null)
  const [showLLM, setShowLLM] = useState(false)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  /** 最近一次提交的告警文本（用于右侧 RCA 面板回填 alert.message） */
  const lastAlertRef = useRef('')

  const panel = useResizablePanel({
    storageKey: 'rca-agent:detail-width',
    defaultWidth: DETAIL_DEFAULT_WIDTH,
    minWidth: 280,
    minOtherWidth: 340,
    maxWidth: 1100,
  })

  // Load existing incidents on mount
  useEffect(() => {
    loadIncidents()
  }, [loadIncidents])

  // 头部常驻显示当前 LLM；失败不打扰用户（模型设置弹窗里会给出明确报错）
  useEffect(() => {
    let alive = true
    llmApi.config().then(c => { if (alive) setLlmCfg(c) }).catch(() => {})
    return () => { alive = false }
  }, [])

  // 从故障注入控制台交接过来的提示词：只填进输入框，由人工确认后再发送
  useEffect(() => {
    if (initialPrompt) setInput(initialPrompt)
  }, [initialPrompt])

  // Auto-switch to RCA tab when a new result arrives
  useEffect(() => {
    if (rcaResult) setRightTab('rca')
  }, [rcaResult])

  // 诊断开始 → 在聊天框里开启"实时推理"气泡（推理已从右侧面板搬进对话）
  useEffect(() => {
    if (agentStreaming) {
      setLiveTurn({ steps: [], route: null, final: null, error: null, streaming: true })
    }
  }, [agentStreaming])

  // 推理流式更新：把 agentStore 的步骤/路由同步到实时气泡
  useEffect(() => {
    if (!agentStreaming) return
    setLiveTurn({ steps: agentSteps, route: agentRoute, final: null, error: null, streaming: true })
  }, [agentSteps, agentRoute, agentStreaming])

  // 桥接：agent 最终结果 →（1）结论落成 assistant 消息（含推理快照）（2）拓扑/指标面板
  useEffect(() => {
    if (!agentFinal) return
    const rc = agentFinal.root_cause
    const snapshot = useAgentStore.getState()
    // 推理步骤来源有两个，必须都考虑：
    //   · agentic 路径：流式 `step_*` 事件累积在 store.steps 里；
    //   · deterministic 快路径：**不发 step 事件**，步骤只随 final 下发。
    const traceSteps = snapshot.steps.length
      ? snapshot.steps
      : normalizeSteps(agentFinal.steps)
    // (1) 把"推理 + 结论"作为一轮完整的 assistant 回复写进对话：
    //     这样历史里每条回答都能展开看到当时的推理，而不是只留在右侧面板。
    useChatStore.setState(s => ({
      messages: [...s.messages, {
        role: 'assistant' as const,
        content: conclusionMarkdown(agentFinal),
        timestamp: Date.now(),
        incident_id: agentFinal.incident_id || undefined,
        trace: traceSteps,
        route: snapshot.routeInfo ?? (agentFinal.route
          ? { mode: agentFinal.mode, reason: agentFinal.route.reason }
          : undefined),
      }],
    }))
    // 结论已落地 → 收起实时气泡，避免与上面的消息重复
    setLiveTurn(null)

    // (2) 结构化结果桥接到右侧面板（RCA / 拓扑 / 指标）
    //
    // ⚠ 这里**必须整体透传** `rc`，不能手工重建一个"只有 5 个字段"的对象。
    // 踩过的坑：原先写成 `{category, entity_id, entity_name: rc.description, confidence, reason}`，
    // 于是后端已经从本体取出来的解释性字段（机制 definition、归因对象 affected、
    // 观测佐证 evidenced_by、判据 constraints、命中关键词 matched_keywords）
    // 在**前端这一层被悄悄丢掉**，面板上的「根因结论」又退回成
    // "资源类 / rc:tmp-disk / 一句话"。
    // 后端返回什么就存什么；只对缺失字段做兜底补全。
    const candidate = rc ? {
      ...rc,
      category: rc.category ?? '未知',
      entity_id: rc.entity_id ?? '',
      // 兼容 LLM 路径：它只给 description，没有本体名称
      entity_name: rc.entity_name || rc.description || '',
      confidence: rc.confidence ?? agentFinal.confidence ?? 0,
      reason: rc.reason || rc.description || '',
    } : null
    useChatStore.setState({
      rcaResult: {
        incident_id: agentFinal.incident_id ?? '',
        alert: {
          message: lastAlertRef.current,
          severity: 'P1',
          keywords: [],
          matched_categories: [],
        },
        evidence: { items: agentFinal.evidence ?? [] } as unknown as Record<string, unknown>,
        thresholds_triggered: [],
        topology: (agentFinal.topology as any[]) ?? [],
        topology_edges: (agentFinal.topology_edges as any[]) ?? [],
        root_cause_path: (agentFinal.root_cause_path as any[]) ?? [],
        candidates: candidate ? [candidate] : [],
        root_cause: candidate,
        rca_chain: [],
        elapsed_ms: agentFinal.latency_ms ?? 0,
        confidence: agentFinal.confidence ?? 0,
        // 处置动作带过来（ontology_v4 起来自本体 remediation，界面会标出处）
        next_actions: (agentFinal.next_actions as never[]) ?? [],
      },
    })
    setRightTab('rca')
  }, [agentFinal])

  // 诊断报错 → 也落成一条 assistant 消息（把失败留在对话里），并收起实时气泡
  useEffect(() => {
    if (!agentError) return
    const st = useAgentStore.getState()
    useChatStore.setState(s => ({
      messages: [...s.messages, {
        role: 'assistant' as const,
        content: `## 诊断失败\n\n\`\`\`\n${agentError}\n\`\`\``,
        timestamp: Date.now(),
        trace: st.steps,
        route: st.routeInfo ?? undefined,
      }],
    }))
    setLiveTurn(null)
  }, [agentError])

  const handleSubmit = async () => {
    if (!input.trim() || isLoading || agentStreaming) return
    const text = input.trim()
    lastAlertRef.current = text
    setInput('')
    setLiveTurn({ steps: [], route: null, final: null, error: null, streaming: true })
    // 追加用户消息到对话窗口（保留上下文观感）
    useChatStore.setState(s => ({
      messages: [...s.messages, { role: 'user', content: text, timestamp: Date.now() }],
    }))
    // 双引擎诊断（流式推理 → 聊天框内实时展示）
    await sendDiagnose(text)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit()
    }
  }

  // 折叠时右侧仅保留标签栏
  const detailStyle: React.CSSProperties = panel.collapsed
    ? { width: 44, minWidth: 44, flexBasis: 44 }
    : { width: panel.width, minWidth: panel.width, flexBasis: panel.width }

  return (
    <div className="agent-layout">
      <header className="agent-header">
        <div className="header-left">
          <span className="logo">🔍</span>
          <h1>RCA Agent</h1>
          <span className="subtitle">信用卡系统运维智能体</span>
        </div>
        <div className="header-right">
          {/* 当前 LLM 常驻显示 + 手动切换入口。
              为什么放在头部：诊断走哪条引擎/哪个模型直接影响结论与成本，
              这属于"当前会话的运行态"，不该藏在设置里。 */}
          <span className={`llm-chip${llmCfg?.runtime.active ? ' override' : ''}`}
                title={llmCfg
                  ? `${llmCfg.provider} / ${llmCfg.model}\n${llmCfg.base_url}` +
                    (llmCfg.runtime.active
                      ? '\n（运行中手动覆盖，重启后端后回到环境变量）'
                      : '\n（来自环境变量配置）')
                  : '读取中…'}>
            🧠 {llmCfg ? `${llmCfg.provider}/${llmCfg.model}` : '…'}
            {llmCfg?.runtime.active ? ' · 手动' : ''}
          </span>
          <button onClick={() => setShowLLM(true)} title="切换 LLM provider / 模型">⚙ 模型</button>
          <span className="session-badge">session: {sessionId.slice(-8)}</span>
          <span className="session-badge">incidents: {incidents.length}</span>
          <button
            onClick={panel.toggleCollapsed}
            title={panel.collapsed ? '展开右侧面板' : '折叠右侧面板'}
          >
            {panel.collapsed ? '⏴ 展开' : '⏵ 折叠'}
          </button>
          <button onClick={() => { setLiveTurn(null); reset() }} title="清空对话">🗑 清空</button>
        </div>
      </header>

      <div
        className={`agent-body${panel.dragging ? ' is-dragging' : ''}`}
        ref={panel.containerRef}
      >
        <div className="panel-chat">
          <ChatWindow messages={messages} isLoading={isLoading} live={liveTurn} />

          <div className="shortcuts">
            {SHORTCUTS.map((s, i) => (
              <button key={i} onClick={() => setInput(s)} className="shortcut-btn" title={s}>
                {s.length > 34 ? s.slice(0, 34) + '…' : s}
              </button>
            ))}
          </div>

          <div className="input-area">
            <textarea
              ref={inputRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="描述告警或故障现象，按 Enter 发送…（Shift+Enter 换行）"
              rows={2}
            />
            <button
              className="primary send-btn"
              onClick={handleSubmit}
              disabled={isLoading || agentStreaming || !input.trim()}
            >
              {agentStreaming ? '分析中…' : isLoading ? '发送中…' : '发送'}
            </button>
          </div>

          {(error || agentError) && (
            <div
              className="error-banner"
              onClick={() => { clearError(); clearAgent() }}
              title="点击关闭"
            >
              ❌ {agentError || error}
            </div>
          )}
        </div>

        {/* ── 可拖拽分隔条 ───────────────────────────────────── */}
        <div
          className={`splitter${panel.dragging ? ' active' : ''}${panel.collapsed ? ' collapsed' : ''}`}
          role="separator"
          aria-orientation="vertical"
          aria-label="调整面板宽度"
          aria-valuenow={panel.collapsed ? 0 : panel.width}
          aria-valuemin={280}
          aria-valuemax={1100}
          tabIndex={0}
          title="拖拽调整宽度 · 双击复位 · ←/→ 微调 · Enter 折叠"
          onMouseDown={panel.onHandleMouseDown}
          onTouchStart={panel.onHandleTouchStart}
          onKeyDown={panel.onHandleKeyDown}
          onDoubleClick={panel.onHandleDoubleClick}
        >
          <span className="splitter-grip" />
        </div>

        <div className={`panel-detail${panel.collapsed ? ' collapsed' : ''}`} style={detailStyle}>
          <div className="detail-tabs">
            <button
              className={rightTab === 'rca' ? 'active' : ''}
              onClick={() => { setRightTab('rca'); if (panel.collapsed) panel.toggleCollapsed() }}
              title="RCA 结果"
            >
              {panel.collapsed ? '📊' : '📊 RCA'}
            </button>
            <button
              className={rightTab === 'topology' ? 'active' : ''}
              onClick={() => { setRightTab('topology'); if (panel.collapsed) panel.toggleCollapsed() }}
              title="拓扑图"
            >
              {panel.collapsed ? '🔗' : '🔗 拓扑'}
            </button>
            <button
              className={rightTab === 'metrics' ? 'active' : ''}
              onClick={() => { setRightTab('metrics'); if (panel.collapsed) panel.toggleCollapsed() }}
              title="监控指标"
            >
              {panel.collapsed ? '📈' : '📈 指标'}
            </button>
            <button
              className={rightTab === 'history' ? 'active' : ''}
              onClick={() => { setRightTab('history'); if (panel.collapsed) panel.toggleCollapsed() }}
              title="历史事件"
            >
              {panel.collapsed ? '🕘' : '🕘 历史'}
            </button>
          </div>

          {!panel.collapsed && (
            <div className="detail-content">
              {rightTab === 'rca' && <RCAResultPanel result={rcaResult} />}
              {rightTab === 'topology' && (
                <TopologyGraph
                  nodes={rcaResult?.topology ?? []}
                  edges={rcaResult?.topology_edges ?? []}
                  path={rcaResult?.root_cause_path ?? []}
                />
              )}
              {rightTab === 'metrics' && <MetricsPanel evidence={rcaResult?.evidence ?? null} />}
              {rightTab === 'history' && (
                <IncidentList incidents={incidents} onSelect={id => useChatStore.getState().selectIncident(id)} />
              )}
            </div>
          )}
        </div>
      </div>

      {showLLM && (
        <LLMSettings
          onClose={() => setShowLLM(false)}
          onChanged={setLlmCfg}
        />
      )}
    </div>
  )
}
