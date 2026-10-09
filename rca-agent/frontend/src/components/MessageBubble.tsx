import type { ChatMessage } from '../stores/chatStore'
import { ReasoningInline } from './ReasoningInline'

interface Props { message: ChatMessage }

export function MessageBubble({ message }: Props) {
  const { role, content, timestamp, tool_calls, trace, route } = message

  const isUser   = role === 'user'
  const isTool   = role === 'tool'
  const isAssistant = role === 'assistant'

  return (
    <div className={`message ${role}`}>
      {/* 推理放在结论气泡**之前**：读起来就是"我先查了这些 → 所以是这个结论"。
          历史回看时默认折叠，需要时展开。 */}
      {isAssistant && trace && trace.length > 0 && (
        <ReasoningInline steps={trace} route={route} defaultOpen={false} />
      )}
      <div className={`bubble ${isUser ? 'user-bubble' : isTool ? 'tool-bubble' : 'assistant-bubble'}`}>
        {isAssistant && content.startsWith('##') ? (
          <MarkdownRender content={content} />
        ) : (
          <p>{content}</p>
        )}
        {timestamp && (
          <span className="timestamp">
            {new Date(timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}
          </span>
        )}
      </div>
      {tool_calls && tool_calls.length > 0 && (
        <div className="tool-calls">
          {tool_calls.map(tc => (
            <span key={tc.id} className="tool-call-badge">🔧 {tc.name}</span>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Markdown-like renderer (simple, no extra deps) ─────────────────────────

function MarkdownRender({ content }: { content: string }) {
  const lines = content.split('\n')
  return (
    <div className="md-render">
      {lines.map((line, i) => {
        if (line.startsWith('## ')) return <h2 key={i}>{line.slice(3)}</h2>
        if (line.startsWith('### ')) return <h3 key={i}>{line.slice(4)}</h3>
        if (line.startsWith('**') && line.endsWith('**')) {
          return <p key={i}><strong>{line.slice(2, -2)}</strong></p>
        }
        if (line.startsWith('- ')) return <li key={i}>{renderInline(line.slice(2))}</li>
        if (line.startsWith('1. ') || line.startsWith('2. ') || line.startsWith('3. ') || line.startsWith('4. ')) {
          return <li key={i}>{renderInline(line.slice(3))}</li>
        }
        if (line.startsWith('|')) return <pre key={i} className="md-table">{line}</pre>
        if (line.trim() === '') return <br key={i} />
        return <p key={i}>{renderInline(line)}</p>
      })}
    </div>
  )
}

function renderInline(text: string) {
  // Bold: **text**
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
  return parts.map((p, i) => {
    if (p.startsWith('**') && p.endsWith('**')) {
      return <strong key={i}>{p.slice(2, -2)}</strong>
    }
    if (p.startsWith('`') && p.endsWith('`')) {
      return <code key={i} style={{ background: '#21262d', padding: '1px 4px', borderRadius: '3px', fontSize: '12px' }}>{p.slice(1, -1)}</code>
    }
    return <span key={i}>{p}</span>
  })
}
