import { useState } from 'react'
import type { AgentFinal, AgentStepView } from '../stores/agentStore'
import './ReasoningInline.css'

/**
 * 内嵌在聊天框里的推理过程。
 *
 * 为什么搬到聊天框：原来的推理只显示在右侧面板，聊天框里只有"我问了什么"，
 * 看起来像自问自答 —— 推理本身就是对话的一部分，应该和问题在同一视线流里。
 *
 * 同一个组件被用在两处：
 *   · **实时**：流式过程中渲染当前这一轮的推理（自动展开，随步骤增长）；
 *   · **历史**：结论落到 assistant 消息时把推理快照存进消息，回看时折叠展示。
 */
export interface ReasoningInlineProps {
  steps: AgentStepView[]
  route?: { mode: string; reason: string } | null
  final?: AgentFinal | null
  error?: string | null
  streaming?: boolean
  /** 历史回看时默认折叠；实时流式默认展开 */
  defaultOpen?: boolean
}

const MODE_LABEL: Record<string, string> = {
  deterministic: '确定性快路径',
  agentic: 'LLM Agent',
}

/** 把 ARGS 压成一行短文本 */
function fmtArgs(args?: Record<string, unknown>): string {
  if (!args || Object.keys(args).length === 0) return ''
  const parts = Object.entries(args).map(([k, v]) => {
    const s = typeof v === 'string' ? v : JSON.stringify(v)
    return `${k}=${s && s.length > 42 ? s.slice(0, 41) + '…' : s}`
  })
  return parts.join(' ')
}

export function ReasoningInline({
  steps, route, final, error, streaming, defaultOpen = true,
}: ReasoningInlineProps) {
  const [open, setOpen] = useState(defaultOpen)
  const hasContent = steps.length > 0 || !!final || !!error
  if (!hasContent && !streaming) return null

  const toolCount = steps.filter(s => s.phase === 'act').length
  const mode = final?.mode ?? route?.mode ?? ''
  const status = final?.status ?? ''
  // 快路径的 act 步骤是"核对阈值"，不是工具调用 —— 文案要跟着模式走
  const actLabel = mode === 'agentic' ? '次工具调用' : '项证据核对'

  return (
    <div className={`ri ${streaming ? 'ri-live' : ''} ${open ? 'ri-open' : ''}`}>
      <button className="ri-head" onClick={() => setOpen(o => !o)} type="button">
        <span className="ri-toggle">{open ? '▾' : '▸'}</span>
        <span className="ri-title">
          {streaming ? '推理中' : '推理过程'}
          {streaming && <span className="ri-dots"><i /><i /><i /></span>}
        </span>
        {mode && <span className={`ri-chip ${mode}`}>{MODE_LABEL[mode] ?? mode}</span>}
        {toolCount > 0 && <span className="ri-chip muted">{toolCount} {actLabel}</span>}
        {!streaming && status && status !== 'completed' && (
          <span className="ri-chip warn">{status}</span>
        )}
        {!streaming && final?.total_tokens ? (
          <span className="ri-chip muted">{final.total_tokens} tokens</span>
        ) : null}
      </button>

      {open && (
        <div className="ri-body">
          {route?.reason && <div className="ri-route">路由判定：{route.reason}</div>}

          <ol className="ri-steps">
            {steps.map((s, i) => (
              <li key={`${s.step}-${s.phase}-${i}`} className={`ri-step ${s.phase}`}>
                {/* 用文字徽章而不是 emoji：不同平台/无 emoji 字体的环境下
                    emoji 会渲染成小方块或小点，反而看不清是哪一类步骤 */}
                <span className="ri-badge">
                  {s.phase === 'reason' ? '推理' : s.phase === 'act' ? (mode === 'agentic' ? '调用' : '核对') : '观察'}
                </span>
                <div className="ri-step-main">
                  {s.phase === 'reason' && (
                    <div className="ri-reason">{s.reasoning || '（思考中…）'}</div>
                  )}
                  {s.phase === 'act' && (
                    <>
                      <div className="ri-tool">
                        <code>{s.tool_name}</code>
                        {fmtArgs(s.tool_args) && <span className="ri-args">{fmtArgs(s.tool_args)}</span>}
                      </div>
                      {s.observation !== undefined && s.observation !== '' && (
                        <div className={`ri-obs ${s.ok === false ? 'bad' : 'ok'}`}>
                          {s.ok === false ? '✘' : '✔'} {s.observation}
                          {s.latency_ms ? <span className="ri-lat"> · {Math.round(s.latency_ms)}ms</span> : null}
                        </div>
                      )}
                    </>
                  )}
                  {s.phase === '' && <div className="ri-reason">{s.reasoning || ''}</div>}
                </div>
              </li>
            ))}
          </ol>

          {streaming && steps.length === 0 && (
            <div className="ri-wait">正在读取本体与多源证据…</div>
          )}

          {final && (
            <div className="ri-conclusion">
              <div className="ri-row">
                <span className="ri-k">根因实体</span>
                <code>{final.root_cause?.entity_id || '—'}</code>
              </div>
              <div className="ri-row">
                <span className="ri-k">置信度</span>
                <span className="ri-v">{final.confidence != null ? final.confidence.toFixed(2) : '—'}</span>
              </div>
              <div className="ri-row">
                <span className="ri-k">与本体先验</span>
                <span className="ri-v">
                  {final.seed_agreement_level
                    ? { exact: '完全一致', category: '类别一致', divergent: '存在分歧' }[
                        final.seed_agreement_level] ?? final.seed_agreement_level
                    : final.seed_agreement === true ? '一致'
                    : final.seed_agreement === false ? '不一致' : '—'}
                </span>
              </div>
              <div className="ri-row">
                <span className="ri-k">耗时</span>
                <span className="ri-v">{Math.round(final.latency_ms || 0)}ms</span>
              </div>
            </div>
          )}

          {error && <div className="ri-error">✘ {error}</div>}
        </div>
      )}
    </div>
  )
}

/** 把 Agent 结论渲染成对话里的 assistant 消息（Markdown 文本） */
export function conclusionMarkdown(final: AgentFinal): string {
  const rc = final.root_cause
  const modeLabel = MODE_LABEL[final.mode] ?? final.mode
  const agree = final.seed_agreement_level
    ? ({ exact: '完全一致', category: '类别一致', divergent: '存在分歧' } as Record<string, string>)[
        final.seed_agreement_level] ?? final.seed_agreement_level
    : final.seed_agreement === true ? '一致' : final.seed_agreement === false ? '不一致' : '—'

  const lines: string[] = []
  lines.push('## 根因判定')
  if (final.status && final.status !== 'completed') {
    lines.push(`> 本次运行状态：**${final.status}**${final.error ? `（${final.error}）` : ''}`)
    lines.push('')
  }
  lines.push(`- **根因实体**：\`${rc?.entity_id || '未定论'}\``)
  if (rc?.entity_name) lines.push(`- **根因名称**：${rc.entity_name}`)
  if (rc?.category) lines.push(`- **类别**：${rc.category}`)
  lines.push(`- **置信度**：${(final.confidence ?? 0).toFixed(2)}`)
  lines.push(`- **诊断模式**：${modeLabel}`)
  lines.push(`- **与本体先验**：${agree}`)
  lines.push(`- **耗时 / 步数 / tokens**：${Math.round(final.latency_ms || 0)}ms / ${final.steps_used ?? 0} 步 / ${final.total_tokens ?? 0}`)

  // ── 让结论能自解释的部分 ────────────────────────────────────────────
  // 以前只给「类别 + rc:xxx + 一句话」，用户看不出机制、落点与判据。
  // 下面这些字段本体里本来就有（经 OntologyScoring.explain 展开），只是没展示。
  if (rc?.definition) {
    lines.push('')
    lines.push('### 机制')
    lines.push(rc.definition)
  }
  if (rc?.affected?.length) {
    lines.push('')
    lines.push('### 归因对象（根因落在哪）')
    rc.affected.forEach(a => {
      lines.push(`- \`${a.id}\`${a.label && a.label !== a.id ? ` ${a.label}` : ''}${a.description ? ` —— ${a.description}` : ''}`)
    })
  }
  if (rc?.evidenced_by?.length) {
    lines.push('')
    lines.push('### 观测佐证')
    rc.evidenced_by.forEach(e => {
      lines.push(`- \`${e.id}\`${e.description ? ` —— ${e.description}` : ''}`)
    })
  }
  if (rc?.constraints?.length) {
    lines.push('')
    lines.push('### 判据（本体约束）')
    rc.constraints.forEach(c => {
      lines.push(`- \`${c.id}\`${c.severity ? `［${c.severity}］` : ''}${c.scope ? ` scope=${c.scope}` : ''}：${c.description || ''}`)
    })
  }
  if (rc?.matched_keywords?.length) {
    lines.push('')
    lines.push(`### 命中依据`)
    lines.push(rc.matched_keywords.map(k => `\`${k}\``).join(' · '))
  }
  if (rc?.description) {
    lines.push('')
    lines.push('### 结论描述')
    lines.push(rc.description)
  }
  if (rc?.reason && rc.reason !== rc.description) {
    lines.push('')
    lines.push('### 判别过程')
    lines.push(rc.reason)
  }
  if (final.reasoning_summary) {
    lines.push('')
    lines.push('### 推理摘要')
    lines.push(final.reasoning_summary)
  }
  if (final.evidence?.length) {
    lines.push('')
    lines.push('### 关键证据')
    final.evidence.slice(0, 6).forEach(e => {
      const val = e.value === undefined || e.value === null ? '' : ` = ${String(e.value)}`
      lines.push(`- \`${e.source || '-'}\` ${e.finding || ''}${val}`)
    })
  }
  if (final.next_actions?.length) {
    lines.push('')
    lines.push('### 建议动作（只读排查，需人工执行）')
    final.next_actions.slice(0, 6).forEach(a => {
      lines.push(`- ${a.urgency ? `**[${a.urgency}]** ` : ''}${a.action || ''}${a.needs_approval ? '（需审批）' : ''}`)
    })
  }
  return lines.join('\n')
}
