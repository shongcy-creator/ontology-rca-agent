import { useCallback, useEffect, useState } from 'react'
import {
  costApi, type CostReport, type EvolutionReport, type RateLimit,
} from '../api/cost'
import './CostPage.css'

/**
 * 成本看板：把后端已有的 `/cost`、`/rate-limit`、`/evolution` 呈现出来。
 *
 * 为什么值得有这一页：本项目的诊断默认走 LLM，单次 2.6 万–6.7 万 token、
 * 48–52s；token 预算是**软上限**（只在进入下一步前检查），
 * 所以"花了多少、有多少次是超预算结束的"必须能一眼看到，
 * 否则成本既不可视也不可约束（见验证手册 §11.21 第 5、17 项）。
 */
export function CostPage() {
  const [cost, setCost] = useState<CostReport | null>(null)
  const [rl, setRl] = useState<RateLimit | null>(null)
  const [evo, setEvo] = useState<EvolutionReport | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [at, setAt] = useState<Date | null>(null)

  const load = useCallback(async () => {
    setBusy(true)
    setErr(null)
    try {
      const [c, r, e] = await Promise.all([
        costApi.cost(), costApi.rateLimit(), costApi.evolution().catch(() => null),
      ])
      setCost(c); setRl(r); setEvo(e); setAt(new Date())
    } catch (e) {
      setErr(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const s = cost?.store
  const n = (v: number | undefined, d = 0) =>
    (v ?? 0).toLocaleString('en-US', { maximumFractionDigits: d })

  /** 超预算结束的比例 —— 这是"软上限"最直接的体现 */
  const budgetPct = s && s.total_runs
    ? Math.round(((s.by_status?.budget_exceeded ?? 0) / s.total_runs) * 100) : 0

  return (
    <div className="cost-page">
      <header className="cost-header">
        <div>
          <h2>💰 成本与限流</h2>
          <span className="cost-sub">
            数据源：<code>/api/agent/cost</code> · <code>/rate-limit</code> · <code>/evolution</code>
            {at && <> · 更新于 {at.toLocaleTimeString()}</>}
          </span>
        </div>
        <button onClick={load} disabled={busy}>{busy ? '加载中…' : '🔄 刷新'}</button>
      </header>

      {err && <div className="cost-err">读取失败：{err}</div>}

      {s && (
        <>
          <section className="cost-kpis">
            <div className="kpi">
              <span className="kpi-v">{n(s.total_runs)}</span>
              <span className="kpi-k">诊断运行次数</span>
            </div>
            <div className="kpi">
              <span className="kpi-v">{n(s.total_tokens)}</span>
              <span className="kpi-k">累计 tokens</span>
            </div>
            <div className="kpi">
              <span className="kpi-v">${(cost?.estimated_cost_usd ?? 0).toFixed(4)}</span>
              <span className="kpi-k">估算费用（USD）</span>
            </div>
            <div className="kpi">
              <span className="kpi-v">{(s.avg_latency_ms / 1000).toFixed(1)}s</span>
              <span className="kpi-k">平均诊断耗时</span>
            </div>
            <div className="kpi">
              <span className="kpi-v">{s.avg_confidence.toFixed(2)}</span>
              <span className="kpi-k">平均置信度</span>
            </div>
            <div className="kpi">
              <span className="kpi-v">{n(s.total_thoughts)}</span>
              <span className="kpi-k">推理步数累计</span>
            </div>
          </section>

          <section className="cost-grid">
            <div className="cost-card">
              <h3>按引擎路径</h3>
              <table>
                <tbody>
                  {Object.entries(s.by_mode || {}).map(([k, v]) => (
                    <tr key={k}>
                      <td>
                        {k === 'deterministic'
                          ? '确定性快路径'
                          : k === 'agentic' ? 'LLM Agent' : k}
                        {k === 'deterministic' && <span className="tag ok">0 token</span>}
                      </td>
                      <td className="num">{n(v)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="cost-hint">
                快路径<strong>不花 token</strong> —— 这也是「本体优先路由」的收益来源。
              </p>
            </div>

            <div className="cost-card">
              <h3>按结束状态</h3>
              <table>
                <tbody>
                  {Object.entries(s.by_status || {}).map(([k, v]) => (
                    <tr key={k}>
                      <td>
                        {k === 'completed' ? '正常完成'
                          : k === 'budget_exceeded' ? '超预算结束'
                            : k === 'fallback' ? '降级返回' : k}
                      </td>
                      <td className="num">{n(v)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {budgetPct > 0 && (
                <p className={`cost-hint${budgetPct >= 20 ? ' warn' : ''}`}>
                  <strong>{budgetPct}%</strong> 的运行因<strong>超预算</strong>结束（token 上限是软约束：
                  只在进入下一步前检查，因此可能超出配置值）。
                </p>
              )}
            </div>

            <div className="cost-card">
              <h3>限流（每分钟）</h3>
              {rl ? (
                <>
                  <div className="rl-bar">
                    <div className="rl-fill"
                         style={{ width: `${Math.min(100, (rl.current_in_window / Math.max(1, rl.max_per_minute)) * 100)}%` }} />
                  </div>
                  <div className="rl-meta">
                    窗口内 <strong>{rl.current_in_window}</strong> / {rl.max_per_minute}
                    <span className="rl-rest">剩余 {rl.remaining}</span>
                  </div>
                  <p className="cost-hint">超过上限时诊断接口返回 429（脚本侧需退避重试）。</p>
                </>
              ) : <p className="cost-hint">无数据</p>}
            </div>

            <div className="cost-card">
              <h3>本体演化就绪度</h3>
              {evo ? (
                <table>
                  <tbody>
                    <tr>
                      <td>新增轨迹</td>
                      <td className="num">
                        {n(evo.new_trajectories)} / {n(evo.threshold_trajectories)}
                        <span className={`tag${evo.trigger_by_trajectories ? ' ok' : ''}`}>
                          {evo.trigger_by_trajectories ? '已达标' : '未达标'}
                        </span>
                      </td>
                    </tr>
                    <tr>
                      <td>距上次演化</td>
                      <td className="num">
                        {evo.days_since_last === null ? '—' : `${evo.days_since_last.toFixed(1)} 天`}
                        <span className={`tag${evo.trigger_by_days ? ' ok' : ''}`}>
                          {evo.trigger_by_days ? `≥${evo.threshold_days}天` : `<${evo.threshold_days}天`}
                        </span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              ) : <p className="cost-hint">无法读取演化状态</p>}
              <p className="cost-hint">
                「就绪」只表示<strong>达到触发条件</strong>，不等于已经跑过一轮演化。
              </p>
            </div>
          </section>

          <section className="cost-card wide">
            <h3>模型单价（每 100 万 tokens，USD）</h3>
            <table className="price-table">
              <thead>
                <tr><th>模型</th><th>输入</th><th>输出</th><th>输出/输入</th></tr>
              </thead>
              <tbody>
                {Object.entries(cost?.pricing_per_1m_tokens || {}).map(([m, p]) => (
                  <tr key={m}>
                    <td><code>{m}</code></td>
                    <td className="num">${p.input}</td>
                    <td className="num">${p.output}</td>
                    <td className="num">{p.input ? `${(p.output / p.input).toFixed(1)}×` : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="cost-hint">
              当前估算按 <strong>总 token ÷ 1e6 × $1.0</strong> 计算（后端口径），
              未区分输入/输出 —— 因此它只是量级参考；按上表分模型精算需要逐次运行的价格明细。
            </p>
          </section>
        </>
      )}
    </div>
  )
}
