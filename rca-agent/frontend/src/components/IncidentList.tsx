import type { IncidentSummary } from '../stores/chatStore'
import './IncidentList.css'

interface Props {
  incidents: IncidentSummary[]
  onSelect: (incidentId: string) => void
}

const CATEGORY_COLORS: Record<string, string> = {
  '数据': '#3fb950',
  '资源': '#d29922',
  '配置': '#58a6ff',
  '依赖': '#bc8cff',
  '代码': '#f85149',
}

const SEVERITY_CLASS: Record<string, string> = {
  P0: 'sev-P0',
  P1: 'sev-P1',
  P2: 'sev-P2',
  P3: 'sev-P3',
}

export function IncidentList({ incidents, onSelect }: Props) {
  if (incidents.length === 0) {
    return (
      <div className="incident-empty">
        <p>🕘 暂无历史分析记录</p>
        <p className="hint">在左侧发送告警后，分析记录会出现在这里</p>
      </div>
    )
  }

  return (
    <div className="incident-list">
      <div className="incident-count">共 {incidents.length} 条分析记录</div>
      {incidents.map(inc => (
        <button
          key={inc.incident_id}
          className="incident-item"
          onClick={() => onSelect(inc.incident_id)}
          title={inc.message}
        >
          <div className="incident-row1">
            <span className={`severity-badge ${SEVERITY_CLASS[inc.severity] || 'sev-P3'}`}>
              {inc.severity}
            </span>
            <span className="incident-id">{inc.incident_id}</span>
            <span className="incident-conf">{Math.round((inc.confidence || 0) * 100)}%</span>
          </div>
          <div className="incident-msg">{inc.message || '(no message)'}</div>
          <div className="incident-row3">
            <span
              className="incident-cat"
              style={{ color: CATEGORY_COLORS[inc.category] || '#8b949e' }}
            >
              [{inc.category || '?'}]
            </span>
            <code className="incident-entity">{inc.entity_id}</code>
          </div>
        </button>
      ))}
    </div>
  )
}
