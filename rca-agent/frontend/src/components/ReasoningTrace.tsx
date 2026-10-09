import { useEffect, useRef } from 'react'
import { useAgentStore, type AgentStepView, type AgentFinal } from '../stores/agentStore'
import './ReasoningTrace.css'

const PHASE_META: Record<string, { icon: string; label: string; color: string }> = {
  observe: { icon: '👁', label: '观察', color: '#58a6ff' },
  reason:  { icon: '🧠', label: '推理', color: '#bc8cff' },
  act:     { icon: '🔧', label: '行动', color: '#d29922' },
}

function FinalCard({ final }: { final: AgentFinal }) {
  const rc = final.root_cause
  const conf = Math.round((final.confidence || 0) * 100)
  const agree = final.seed_agreement
  return (
    <div className="rt-final">
      <div className="rt-final-header">
        <span className={`rt-status rt-status-${final.status}`}>{final.status}</span>
        <span className="rt-mode">{final.mode === 'deterministic' ? '确定性引擎' : 'LLM Agent'}</span>
        <span className="rt-cost">tokens={final.total_tokens} · {Math.round(final.latency_ms / 1000)}s</span>
      </div>

      {rc && (rc.category || rc.entity_id) && (
        <div className="rt-rootcause">
          <div className="rt-rc-title">根因</div>
          <div className="rt-rc-body">
            <span className="rt-rc-cat">[{rc.category}]</span>
            <code className="rt-rc-entity">{rc.entity_id}</code>
            <span className="rt-rc-conf">{conf}%</span>
          </div>
          {rc.description && <div className="rt-rc-desc">{rc.description}</div>}
        </div>
      )}

      {final.seed_agreement !== null && final.seed_agreement !== undefined && (
        <div className={`rt-agree rt-agree-${agree ? 'yes' : 'no'}`}>
          与本体结论{agree ? '一致' : '不一致'}
          {final.seed_agreement_level ? `（${final.seed_agreement_level}）` : ''}
        </div>
      )}

      {final.evidence && final.evidence.length > 0 && (
        <div className="rt-evidence">
          <div className="rt-section-title">证据链（{final.evidence.length}）</div>
          {final.evidence.map((e, i) => (
            <div key={i} className="rt-ev-item">
              <span className="rt-ev-source">[{e.source}]</span>
              <span className="rt-ev-finding">{e.finding}</span>
              {e.value !== undefined && e.value !== null && e.value !== '' && (
                <span className="rt-ev-value">{String(e.value)}</span>
              )}
            </div>
          ))}
        </div>
      )}

      {final.next_actions && final.next_actions.length > 0 && (
        <div className="rt-actions">
          <div className="rt-section-title">建议动作（需人工确认）</div>
          {final.next_actions.map((a, i) => (
            <div key={i} className="rt-action">
              <span className={`rt-action-urgency rt-urgency-${a.urgency}`}>{a.urgency}</span>
              <span className="rt-action-text">{a.action}</span>
              {a.needs_approval && <span className="rt-action-approve">⛔ 需确认</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function ReasoningTrace() {
  const { streaming, steps, currentStep, routeInfo, final, error } = useAgentStore()
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [steps, final, streaming])

  if (!streaming && !final && !error && steps.length === 0) {
    return (
      <div className="rt-empty">
        <p>🧠 暂无推理过程</p>
        <p className="hint">在左侧发送告警后，Agent 的「观察 → 推理 → 行动」轨迹会实时显示在这里</p>
      </div>
    )
  }

  return (
    <div className="rt-container">
      {/* 路由信息 */}
      {routeInfo && (
        <div className="rt-route">
          <span className={`rt-route-mode rt-route-${routeInfo.mode}`}>
            {routeInfo.mode === 'deterministic' ? '⚡ 确定性快路径' : '🤖 LLM Agent'}
          </span>
          <span className="rt-route-reason">{routeInfo.reason}</span>
        </div>
      )}

      {/* 推理步骤时间线 */}
      <div className="rt-timeline">
        {steps.map((s, i) => {
          const meta = PHASE_META[s.phase] || { icon: '•', label: s.phase, color: '#8b949e' }
          return (
            <div key={i} className={`rt-step rt-step-${s.phase}`}>
              <div className="rt-step-marker" style={{ borderColor: meta.color }}>
                <span style={{ color: meta.color }}>{meta.icon}</span>
              </div>
              <div className="rt-step-body">
                <div className="rt-step-head">
                  <span className="rt-step-label" style={{ color: meta.color }}>{meta.label}</span>
                  {s.latency_ms ? <span className="rt-step-ms">{s.latency_ms}ms</span> : null}
                </div>

                {s.phase === 'reason' && s.reasoning && (
                  <div className="rt-reasoning">{s.reasoning}</div>
                )}

                {s.phase === 'act' && (
                  <div className="rt-act">
                    <div className="rt-act-tool">
                      🔧 <code>{s.tool_name}</code>
                      {s.tool_args && Object.keys(s.tool_args).length > 0 && (
                        <span className="rt-act-args">{JSON.stringify(s.tool_args)}</span>
                      )}
                    </div>
                    {s.observation && (
                      <div className={`rt-observation ${s.ok ? 'rt-obs-ok' : 'rt-obs-err'}`}>
                        {s.observation}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          )
        })}

        {/* 推理中指示 */}
        {streaming && (
          <div className="rt-step rt-step-reason">
            <div className="rt-step-marker" style={{ borderColor: PHASE_META.reason.color }}>
              <span className="rt-spinner" />
            </div>
            <div className="rt-step-body">
              <div className="rt-reasoning rt-thinking">
                正在推理<span className="rt-dots"><span>.</span><span>.</span><span>.</span></span>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* 最终结论 */}
      {final && <FinalCard final={final} />}

      {/* 错误 */}
      {error && (
        <div className="rt-error">
          ❌ {error}
        </div>
      )}

      <div ref={bottomRef} />
    </div>
  )
}
