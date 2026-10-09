import { useEffect, useRef } from 'react'
import type { ChatMessage } from '../stores/chatStore'
import type { AgentFinal, AgentStepView } from '../stores/agentStore'
import { MessageBubble } from './MessageBubble'
import { ReasoningInline } from './ReasoningInline'

/** 当前这一轮的实时推理（结论还没落成 assistant 消息时显示在对话末尾） */
export interface LiveTurn {
  steps: AgentStepView[]
  route: { mode: string; reason: string } | null
  final: AgentFinal | null
  error: string | null
  streaming: boolean
}

interface Props {
  messages: ChatMessage[]
  isLoading: boolean
  live?: LiveTurn | null
}

export function ChatWindow({ messages, isLoading, live }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isLoading, live?.steps.length, live?.final])

  if (messages.length === 0 && !live) {
    return (
      <div className="chat-welcome">
        <div className="welcome-icon">🔍</div>
        <h2>RCA Agent — 信用卡系统运维智能体</h2>
        <p>描述您遇到的告警或故障现象，Agent 将自动进行根因分析。</p>
        <div className="welcome-examples">
          <h3>示例场景：</h3>
          <ul>
            <li>💳 payment-app P99 延迟超过 500ms，MySQL 连接池耗尽</li>
            <li>🧵 容器 OOM 重启，内存使用率 98%</li>
            <li>📊 MySQL 慢查询增多，数据库响应变慢</li>
            <li>⚠️ 5xx 错误率突增，请求大量失败</li>
          </ul>
        </div>
      </div>
    )
  }

  return (
    <div className="chat-messages">
      {messages.map((msg, i) => (
        <MessageBubble key={i} message={msg} />
      ))}

      {/* 实时推理：问题与推理在同一视线流里，而不是"只看到我问了什么" */}
      {live && (live.streaming || live.error) && (
        <div className="message assistant">
          <ReasoningInline
            steps={live.steps}
            route={live.route}
            final={live.final}
            error={live.error}
            streaming={live.streaming}
            defaultOpen
          />
        </div>
      )}

      {isLoading && (
        <div className="message assistant">
          <div className="bubble loading">
            <span className="dot" />
            <span className="dot" />
            <span className="dot" />
            分析中...
          </div>
        </div>
      )}
      <div ref={bottomRef} />
    </div>
  )
}
