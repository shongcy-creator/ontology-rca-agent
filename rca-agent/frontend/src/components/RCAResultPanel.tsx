import type { RCAResult } from '../stores/chatStore'
import './RCAResultPanel.css'

interface Props { result: RCAResult | null }

const CATEGORY_COLORS: Record<string, string> = {
  '数据': '#3fb950',
  '资源': '#d29922',
  '配置': '#58a6ff',
  '依赖': '#bc8cff',
  '代码': '#f85149',
}

export function RCAResultPanel({ result }: Props) {
  if (!result) {
    return (
      <div className="rca-empty">
        <p>📊 暂无 RCA 分析结果</p>
        <p className="hint">在左侧输入告警描述后，根因分析结果将显示在此处</p>
      </div>
    )
  }

  const rc = result.root_cause
  const conf = result.confidence ?? rc?.confidence ?? 0
  const confPct = Math.round(conf * 100)

  return (
    <div className="rca-panel">
      {/* Incident header */}
      <div className="rca-header">
        <div className="incident-id">ID: {result.incident_id}</div>
        <div className="incident-meta">
          <span className={`severity-badge sev-${result.alert.severity}`}>
            {result.alert.severity}
          </span>
          <span className="elapsed">{result.elapsed_ms}ms</span>
        </div>
      </div>

      {/* Confidence gauge */}
      <div className="confidence-section">
        <div className="conf-label">
          置信度 <span className="conf-pct" style={{ color: confPct > 80 ? '#3fb950' : confPct > 60 ? '#d29922' : '#f85149' }}>
            {confPct}%
          </span>
        </div>
        <div className="conf-bar-bg">
          <div
            className="conf-bar-fill"
            style={{
              width: `${confPct}%`,
              background: confPct > 80 ? '#3fb950' : confPct > 60 ? '#d29922' : '#f85149',
            }}
          />
        </div>
      </div>

      {/* Root cause */}
      {rc && (
        <div className="root-cause-card">
          <div className="rc-title">⚡ 根因结论</div>

          {/* 标题用**可读名称**，不再只给一个 `rc:xxx` 代号 */}
          <div className="rc-name" style={{ color: CATEGORY_COLORS[rc.category] || '#e6edf3' }}>
            {rc.entity_name || rc.entity_id}
          </div>

          <div className="rc-chips">
            <span className="rc-chip">{rc.category} 类</span>
            <span className="rc-chip mono"><code>{rc.entity_id}</code></span>
            <span className="rc-chip">置信度 {Math.round((rc.confidence ?? conf) * 100)}%</span>
            {rc.role && <span className="rc-chip">本体角色 {rc.role}</span>}
            {rc.lifecycle && <span className="rc-chip">状态 {rc.lifecycle}</span>}
            {rc.scope && <span className="rc-chip">作用域 {rc.scope}</span>}
          </div>

          {/* 机制：这个根因在技术上是怎么回事 */}
          {rc.definition && (
            <div className="rc-fact">
              <span className="rc-k">机制</span>
              <span className="rc-v">{rc.definition}</span>
            </div>
          )}

          {/* 归因对象：根因落在哪个实例上（原先结论完全没说"在哪"） */}
          {rc.affected && rc.affected.length > 0 && (
            <div className="rc-fact">
              <span className="rc-k">归因对象</span>
              <span className="rc-v">
                {rc.affected.map(a => (
                  <span key={a.id} className="rc-ref" title={a.condition || ''}>
                    <code>{a.id}</code>{a.label && a.label !== a.id ? ` ${a.label}` : ''}
                  </span>
                ))}
              </span>
            </div>
          )}

          {/* 观测佐证 */}
          {rc.evidenced_by && rc.evidenced_by.length > 0 && (
            <div className="rc-fact">
              <span className="rc-k">观测佐证</span>
              <span className="rc-v">
                {rc.evidenced_by.map(e => (
                  <span key={e.id} className="rc-ref" title={e.description || e.condition || ''}>
                    <code>{e.id}</code>
                  </span>
                ))}
              </span>
            </div>
          )}

          {/* 判据：约束给出的阈值/容量口径 */}
          {rc.constraints && rc.constraints.length > 0 && (
            <div className="rc-fact">
              <span className="rc-k">判据</span>
              <span className="rc-v">
                {rc.constraints.map(c => (
                  <span key={c.id} className="rc-cons">
                    <code>{c.id}</code>
                    {c.severity ? `［${c.severity}］` : ''} {c.description}
                  </span>
                ))}
              </span>
            </div>
          )}

          {/* 命中依据：可审计的打分关键词 */}
          {rc.matched_keywords && rc.matched_keywords.length > 0 && (
            <div className="rc-kw">
              <span className="rc-k">命中依据</span>
              {rc.matched_keywords.map(k => <span key={k} className="kw-chip">{k}</span>)}
            </div>
          )}

          {/* LLM 路径的自由描述 */}
          {rc.description && <div className="rc-reason">{rc.description}</div>}

          {/* 完整结论句子（本体驱动的解释链） */}
          {rc.reason && (
            <details className="rc-trace" open={!rc.description}>
              <summary>判别过程</summary>
              <div className="rc-reason">{rc.reason}</div>
            </details>
          )}
        </div>
      )}

      {/* 建议动作：ontology_v4 起由根因术语的 remediation 提供 */}
      {result.next_actions && result.next_actions.length > 0 && (
        <div className="section">
          <div className="section-title">
            🛠 建议动作（只读排查 · 写操作需人工审批）
            {result.next_actions.every(a => a.source === 'ontology')
              ? <span className="act-source ontology">来自本体 remediation</span>
              : <span className="act-source fallback">按类别的兜底建议</span>}
          </div>
          <div className="action-list">
            {result.next_actions.map((a, i) => (
              <div key={i} className="action-row">
                <span className={`act-urgency u-${a.urgency}`}>{a.urgency}</span>
                <div className="act-body">
                  <div className="act-text">{a.action}</div>
                  {a.command && <code className="act-cmd">{a.command}</code>}
                </div>
                <span className={`act-approval ${a.needs_approval ? 'need' : 'readonly'}`}>
                  {a.needs_approval ? '需审批' : '只读'}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Root cause propagation path */}
      {result.root_cause_path && result.root_cause_path.length > 0 && (
        <div className="section">
          <div className="section-title">🧭 根因传播路径</div>
          <div className="rc-path">
            {[result.root_cause_path[0].from, ...result.root_cause_path.map(p => p.to)].map((id, i) => (
              <span key={i} className="rc-path-seg">
                {i > 0 && <span className="rc-path-arrow">→</span>}
                <code className={i === 0 ? 'rc-path-root' : ''}>{id}</code>
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Candidates */}
      {result.candidates.length > 0 && (
        <div className="section">
          <div className="section-title">📋 候选根因</div>
          {result.candidates.map((c, i) => (
            <div key={i} className="candidate-row">
              <span className="cand-rank">{i + 1}</span>
              <span className="cand-cat" style={{ color: CATEGORY_COLORS[c.category] || '#e6edf3' }}>
                [{c.category}]
              </span>
              <code className="cand-id">{c.entity_id}</code>
              <span className="cand-conf">{c.confidence}</span>
            </div>
          ))}
        </div>
      )}

      {/* RCA Chain */}
      {result.rca_chain.length > 0 && (
        <div className="section">
          <div className="section-title">🔗 推理链</div>
          <div className="rca-chain">
            {result.rca_chain.map((step, i) => (
              <div key={i} className={`chain-step type-${step.type}`}>
                <div className="step-num">{step.step}</div>
                <div className="step-body">
                  <span className="step-type">{step.type}</span>
                  <p className="step-desc">{step.description}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Thresholds */}
      {result.thresholds_triggered?.length > 0 && (
        <div className="section">
          <div className="section-title">🚨 告警阈值触发</div>
          {result.thresholds_triggered.map((t, i) => (
            <div key={i} className="threshold-row">
              <span className="t-status">🔴</span>
              <span className="t-rule">{t.rule}</span>
              <span className="t-value">{String(t.value)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
