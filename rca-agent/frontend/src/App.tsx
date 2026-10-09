import { useEffect, useState } from 'react'
import { AgentPage } from './pages/AgentPage'
import { FaultInjectionPage } from './pages/FaultInjectionPage'
import { StressPage } from './pages/StressPage'
import { CostPage } from './pages/CostPage'
import { runningJob } from './state/runningJob'
import './App.css'

type Tab = 'agent' | 'chaos' | 'stress' | 'cost'

const TAB_HASH: Record<Tab, string> = {
  agent: 'agent', chaos: 'chaos', stress: 'stress', cost: 'cost',
}

/** 从 URL hash 解析当前页（支持深链 / 书签 / 刷新后停留在同一页） */
function tabFromHash(): Tab {
  // 容错：hash 上可能带参数或尾斜杠（如 `#cost?from=chaos`、`#cost/`）。
  // 踩过的坑：验证脚本用 `#cost?t=1` 做缓存穿透，结果这里匹配不上、
  // 静默回退到 agent 页 —— 页面"空白"看起来像组件坏了，其实是解析太脆。
  const h = window.location.hash.replace(/^#\/?/, '').split('?')[0].replace(/\/+$/, '')
  if (h === 'chaos') return 'chaos'
  if (h === 'stress') return 'stress'
  if (h === 'cost') return 'cost'
  return 'agent'
}

export function App() {
  const [tab, setTab] = useState<Tab>(tabFromHash)
  // 故障注入台 → 诊断台的提示词交接（刻意用中性文案，不泄露标准答案）
  const [handoff, setHandoff] = useState('')
  // 当前是否有任务在跑（压测/注入）。切页会卸载页面，但任务在后端继续跑；
  // 顶上这个全局指示让用户在任何页面都能看到"还在跑"，并能一键跳回去看进度。
  const [busyJob, setBusyJob] = useState(() => runningJob.get())

  useEffect(() => runningJob.subscribe(setBusyJob), [])

  useEffect(() => {
    const onHash = () => setTab(tabFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const go = (t: Tab) => {
    if (window.location.hash.replace(/^#\/?/, '') !== TAB_HASH[t]) window.location.hash = TAB_HASH[t]
    setTab(t)
  }

  const busyTab: Tab = busyJob?.kind === 'stress' ? 'stress' : 'chaos'

  return (
    <div className="app-shell">
      <nav className="app-nav">
        <span className="app-brand">🛠 credit-card-sys-ops</span>
        <button className={tab === 'agent' ? 'on' : ''} onClick={() => go('agent')}>
          🔍 智能诊断
        </button>
        <button className={tab === 'chaos' ? 'on' : ''} onClick={() => go('chaos')}>
          ⚡ 故障注入
        </button>
        <button className={tab === 'stress' ? 'on' : ''} onClick={() => go('stress')}>
          📈 压测
        </button>
        <button className={tab === 'cost' ? 'on' : ''} onClick={() => go('cost')}>
          💰 成本
        </button>
        {busyJob && tab !== busyTab && (
          <button className="app-busy" onClick={() => go(busyTab)}
                  title={`${busyJob.id} 正在运行；切页不会中断任务，点这里回到进度页`}>
            ⏳ {busyJob.kind === 'stress' ? '压测' : '任务'}运行中 · {busyJob.label} · 查看
          </button>
        )}
        <span className="app-note">
          诊断只读 · 注入/压测只写 —— 注入记录不进入诊断上下文，保证"智能体不知道答案"
        </span>
      </nav>
      <div className="app-body">
        {tab === 'agent' && <AgentPage initialPrompt={handoff} />}
        {tab === 'chaos' && (
          <FaultInjectionPage
            onDiagnose={(prompt) => {
              setHandoff(prompt)
              go('agent')
            }}
          />
        )}
        {tab === 'stress' && <StressPage />}
        {tab === 'cost' && <CostPage />}
      </div>
    </div>
  )
}
