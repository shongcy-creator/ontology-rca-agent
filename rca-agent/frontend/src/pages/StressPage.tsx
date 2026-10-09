import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { runningJob } from '../state/runningJob'
import {
  chaosApi, stressApi,
  type Job, type JobLog, type StressOptions, type StressParams,
  type StressPreset, type StressSummary,
} from '../api/chaos'
import './StressPage.css'

/**
 * 压测台（`tools/stress_harness.py` 的界面外壳）。
 *
 * 复用故障注入控制台的**任务引擎**：同一套单飞锁 / 实时日志 / 停止按钮，
 * 所以"先注入故障 → 再压测 → 再回滚"这个正确用法天然成立
 * （注入任务结束后故障仍然生效，压测在它之后跑，信号不会互相污染）。
 */

/** 时序列小图（不引入图表库，直接画 SVG 折线） */
function Spark({ data, color, height = 34 }: {
  data: number[]; color: string; height?: number
}) {
  if (data.length < 2) return <div className="spark-empty">样本不足</div>
  const w = 100
  const max = Math.max(...data, 1)
  const min = Math.min(...data, 0)
  const span = max - min || 1
  const pts = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w
    const y = height - ((v - min) / span) * (height - 4) - 2
    return `${x.toFixed(2)},${y.toFixed(2)}`
  }).join(' ')
  return (
    <svg className="spark" viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none">
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.2" />
    </svg>
  )
}

function fmt(n: unknown, digits = 1): string {
  const v = Number(n)
  return Number.isFinite(v) ? v.toFixed(digits) : '—'
}

/** 一次压测的结果卡片 */
function ResultCard({ s, onOpenDetail }: { s: StressSummary; onOpenDetail?: () => void }) {
  const req = s.requests || { total: 0, success: 0, error: 0, success_rate: 0 }
  const lat = s.latency_ms || {}
  const thr = s.throughput || { requests_per_sec: 0, bytes_per_sec: 0, bytes_total: 0 }
  const ts = s.time_series || []
  const errRate = req.total > 0 ? (req.error / req.total) * 100 : 0
  const targetText = s.target
    ? (s.target.mysql ? `MySQL · ${s.target.mysql_query || ''}` : `${s.target.method} ${s.target.url}${s.target.path}`)
    : ''

  return (
    <div className="stress-result">
      <div className="sr-head">
        <strong>{s.label || s.mode}</strong>
        <span className="sr-meta">
          {s.mode} · 并发 {s.concurrency} · {fmt(s.wall_seconds)}s · {s.started_at}
        </span>
        {onOpenDetail && <button className="link" onClick={onOpenDetail}>明细</button>}
      </div>
      <div className="sr-target">{targetText}</div>

      <div className="sr-grid">
        <div className="sr-cell">
          <div className="sr-k">请求 / 失败</div>
          <div className="sr-v">{req.total.toLocaleString()} <span className={req.error ? 'bad' : 'ok'}>/ {req.error}</span></div>
          <div className="sr-sub">成功率 {(req.success_rate * 100).toFixed(2)}%（失败 {errRate.toFixed(2)}%）</div>
        </div>
        <div className="sr-cell">
          <div className="sr-k">吞吐</div>
          <div className="sr-v">{fmt(thr.requests_per_sec)} <small>rps</small></div>
          <div className="sr-sub">{(Number(thr.bytes_per_sec) / 1024).toFixed(1)} KB/s</div>
        </div>
        <div className="sr-cell">
          <div className="sr-k">延迟 p50 / p95 / p99</div>
          <div className="sr-v">{fmt(lat.p50)} / {fmt(lat.p95)} / {fmt(lat.p99)} <small>ms</small></div>
          <div className="sr-sub">min {fmt(lat.min)} · max {fmt(lat.max)} · mean {fmt(lat.mean)}</div>
        </div>
      </div>

      {ts.length > 1 && (
        <div className="sr-charts">
          <div className="sr-chart">
            <span className="sr-chart-label">每秒请求数（max {Math.max(...ts.map(p => p.count))}）</span>
            <Spark data={ts.map(p => p.count)} color="#58a6ff" />
          </div>
          <div className="sr-chart">
            <span className="sr-chart-label">每秒 p99 延迟 ms（max {fmt(Math.max(...ts.map(p => p.p99_ms)))}）</span>
            <Spark data={ts.map(p => p.p99_ms)} color="#d29922" />
          </div>
          <div className="sr-chart">
            <span className="sr-chart-label">每秒错误数（合计 {ts.reduce((a, p) => a + p.errors, 0)}）</span>
            <Spark data={ts.map(p => p.errors)} color="#f85149" />
          </div>
        </div>
      )}

      {(Object.keys(s.status_codes || {}).length > 0 || Object.keys(s.error_kinds || {}).length > 0) && (
        <div className="sr-codes">
          {Object.entries(s.status_codes || {}).map(([k, v]) => (
            <span key={k} className={`code ${k.startsWith('2') ? 'ok' : 'bad'}`}>{k}: {v}</span>
          ))}
          {Object.entries(s.error_kinds || {}).map(([k, v]) => (
            <span key={k} className="code bad" title={k}>{String(k).slice(0, 40)}: {v}</span>
          ))}
        </div>
      )}
    </div>
  )
}

export function StressPage() {
  const [opts, setOpts] = useState<StressOptions | null>(null)
  const [p, setP] = useState<Partial<StressParams>>({})
  const [job, setJob] = useState<Job | null>(null)
  const [logs, setLogs] = useState<JobLog[]>([])
  const [running, setRunning] = useState(false)
  const [banner, setBanner] = useState<{ kind: 'info' | 'error' | 'warn'; text: string } | null>(null)
  const [pendingConfirm, setPendingConfirm] = useState<{ impact: string } | null>(null)
  const [result, setResult] = useState<StressSummary | null>(null)
  const [history, setHistory] = useState<StressSummary[]>([])
  const [detail, setDetail] = useState<StressSummary | null>(null)

  const logBoxRef = useRef<HTMLDivElement>(null)
  const logOffsetRef = useRef(0)
  const pollRef = useRef<number | null>(null)

  useEffect(() => {
    ;(async () => {
      try {
        const o = await stressApi.options()
        setOpts(o)
        setP({ ...o.defaults })
        setHistory(o.history || [])
      } catch (e) {
        setBanner({ kind: 'error', text: String((e as Error).message) })
      }
    })()
  }, [])

  useEffect(() => {
    const el = logBoxRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [logs.length])

  const pollJob = useCallback((jobId: string, fresh = true) => {
    if (pollRef.current) window.clearInterval(pollRef.current)
    // fresh=false 表示"接上一个已经在跑的任务"（切页/刷新回来）：
    // 此时不能清空已有日志，否则用户看到的是"从头开始"，反而更像任务被重启了。
    if (fresh) {
      logOffsetRef.current = 0
      setLogs([])
    }
    setRunning(true)
    pollRef.current = window.setInterval(async () => {
      try {
        const j = await chaosApi.job(jobId, logOffsetRef.current)
        setJob(j)
        if (j.logs?.length) {
          logOffsetRef.current = j.log_offset ?? logOffsetRef.current + j.logs.length
          setLogs(prev => [...prev, ...j.logs!])
        }
        const done = j.status !== 'running' && j.status !== 'queued'
        setRunning(!done)
        if (done) {
          if (pollRef.current) window.clearInterval(pollRef.current)
          pollRef.current = null
          runningJob.finish(jobId)
          const s = (j.result as { summary?: StressSummary })?.summary
          if (s) setResult(s)
          setHistory(await stressApi.history(20).then(r => r.history).catch(() => []))
          if (j.status === 'cancelled') setBanner({ kind: 'warn', text: '压测已停止' })
          else if (j.status === 'failed') setBanner({ kind: 'error', text: j.error || '压测失败' })
          else setBanner({ kind: 'info', text: '压测完成' })
        }
      } catch (e) {
        if (pollRef.current) window.clearInterval(pollRef.current)
        pollRef.current = null
        setRunning(false)
        runningJob.finish(jobId)
        setBanner({ kind: 'error', text: String((e as Error).message) })
      }
    }, 1000)
  }, [])

  // 切页/刷新回来时**接上**正在跑的任务（或显示刚结束那一次的结果）。
  // 这段是本次修复的核心：任务跑在后端，页面不该因为卸载就"看起来没在跑"。
  useEffect(() => {
    const saved = runningJob.get()
    if (!saved || saved.kind !== 'stress') return
    ;(async () => {
      try {
        const j = await chaosApi.job(saved.id, 0)
        const done = j.status !== 'running' && j.status !== 'queued'
        setJob(j)
        if (j.logs?.length) {
          logOffsetRef.current = j.log_offset ?? j.logs.length
          setLogs(j.logs)
        }
        if (done) {
          runningJob.finish(saved.id)
          const s = (j.result as { summary?: StressSummary })?.summary
          if (s) setResult(s)
          setBanner({ kind: 'info', text: `上次压测（${saved.label}）已完成` })
        } else {
          setBanner({ kind: 'info', text: `已接上正在运行的压测（${saved.label}）` })
          pollJob(saved.id, false)
        }
      } catch {
        // 任务在服务端已不存在（例如后端重启）：清掉这条记录，避免一直显示幽灵任务
        runningJob.finish(saved.id)
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current) }, [])

  const start = useCallback(async (confirm = false) => {
    setBanner(null)
    setResult(null)
    try {
      const r = await stressApi.run(p, confirm)
      if (r.need_confirm) {
        setPendingConfirm({ impact: r.impact || r.error || '影响面较大' })
        return
      }
      if (!r.ok || !r.job_id) {
        setBanner({ kind: 'warn', text: r.error || '提交失败' })
        return
      }
      setPendingConfirm(null)
      setRunning(true)
      setJob(null)
      // 先落盘再轮询：万一用户在启动瞬间切走/刷新，回来时也能接上这个任务。
      runningJob.start({ id: r.job_id, kind: 'stress',
                         label: `${p.mode || 'http'}·${p.concurrency}并发·${p.duration}s` })
      pollJob(r.job_id)
    } catch (e) {
      const err = e as Error & { body?: { error?: string; impact?: string; need_confirm?: boolean } }
      if (err.body?.need_confirm) {
        setPendingConfirm({ impact: err.body.impact || err.body.error || '' })
        return
      }
      setBanner({ kind: 'error', text: err.body?.error || err.message })
    }
  }, [p, pollJob])

  const applyPreset = useCallback((preset: StressPreset) => {
    setP(prev => ({ ...prev, ...preset.params }))
    setBanner({ kind: 'info', text: `已套用预设：${preset.label}` })
  }, [])

  const targets = opts?.targets || []
  const maxConc = opts?.limits?.concurrency?.max ?? 256
  const maxDur = opts?.limits?.duration?.max ?? 900

  const needHttp = p.mode === 'http' || p.mode === 'mixed'
  const needMysql = p.mode === 'mysql' || p.mode === 'mixed'
  const activeTargets = useMemo(
    () => targets.filter(t => (p.mode === 'mysql' ? t.kind === 'mysql' : t.kind === 'http')),
    [targets, p.mode])

  useEffect(() => {
    // 切换模式后如果当前目标与新模式的类型不符，自动选第一个可用目标
    if (!opts) return
    const cur = targets.find(t => t.id === p.target)
    const want = p.mode === 'mysql' ? 'mysql' : 'http'
    if (!cur || cur.kind !== want) {
      const first = targets.find(t => t.kind === want)
      if (first) setP(prev => ({ ...prev, target: first.id }))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.mode, opts])

  return (
    <div className="stress-page">
      <header className="stress-header">
        <div className="stress-title">
          <span className="stress-logo">📈</span>
          <h1>压测台</h1>
          <span className="stress-sub">tools/stress_harness.py · 与故障注入共用任务引擎（单飞锁 / 实时日志 / 可停止）</span>
        </div>
        <div className="stress-actions">
          {running ? (
            <button className="stop" onClick={() => chaosApi.stopAndRollback().then(() => setBanner({ kind: 'warn', text: '已请求停止压测' }))}>
              ⏹ 停止压测
            </button>
          ) : (
            <button className="primary" onClick={() => start()}>▶ 开始压测</button>
          )}
          <button onClick={() => stressApi.history(20).then(r => setHistory(r.history))}>刷新历史</button>
        </div>
      </header>

      {banner && (
        <div className={`stress-banner ${banner.kind}`}>
          {banner.text}
          <button className="link" onClick={() => setBanner(null)}>关闭</button>
        </div>
      )}

      <div className="stress-body">
        <aside className="stress-side">
          <section className="sc-card">
            <h2>一键预设</h2>
            <div className="preset-list">
              {(opts?.presets || []).map(x => (
                <button key={x.id} onClick={() => applyPreset(x)} className={x.confirm ? 'danger' : ''}
                        title={JSON.stringify(x.params)}>
                  {x.label}{x.confirm ? ' ⚠' : ''}
                </button>
              ))}
            </div>
          </section>

          <section className="sc-card">
            <h2>参数</h2>
            <div className="form-row">
              <label>模式</label>
              <select value={p.mode} onChange={e => setP(v => ({ ...v, mode: e.target.value as StressParams['mode'] }))}>
                {(opts?.modes || []).map(m => <option key={m.id} value={m.id}>{m.label}</option>)}
              </select>
            </div>
            <div className="form-row">
              <label>目标</label>
              <select value={p.target} onChange={e => setP(v => ({ ...v, target: e.target.value }))}>
                {activeTargets.map(t => <option key={t.id} value={t.id}>{t.label}</option>)}
              </select>
            </div>
            <div className="form-row">
              <label>并发</label>
              <input type="number" min={1} max={maxConc} value={p.concurrency ?? 16}
                     onChange={e => setP(v => ({ ...v, concurrency: Number(e.target.value) }))} />
              <span className="hint">1–{maxConc}</span>
            </div>
            <div className="form-row">
              <label>时长 (s)</label>
              <input type="number" min={1} max={maxDur} value={p.duration ?? 30}
                     onChange={e => setP(v => ({ ...v, duration: Number(e.target.value) }))} />
              <span className="hint">1–{maxDur}</span>
            </div>
            <div className="form-row">
              <label>爬坡 (s)</label>
              <input type="number" min={0} max={120} value={p.ramp_up ?? 0}
                     onChange={e => setP(v => ({ ...v, ramp_up: Number(e.target.value) }))} />
            </div>
            <div className="form-row">
              <label>标签</label>
              <input type="text" value={p.label ?? ''} placeholder="便于归档区分"
                     onChange={e => setP(v => ({ ...v, label: e.target.value }))} />
            </div>

            {needHttp && (
              <>
                <div className="form-row">
                  <label>路径 / 方法</label>
                  <input type="text" value={p.path ?? '/txn'} style={{ flex: 2 }}
                         onChange={e => setP(v => ({ ...v, path: e.target.value }))} />
                  <select value={p.method ?? 'POST'} onChange={e => setP(v => ({ ...v, method: e.target.value }))}>
                    <option>POST</option><option>GET</option>
                  </select>
                </div>
                <div className="form-row">
                  <label>请求体</label>
                  <input type="text" value={p.body ?? ''} onChange={e => setP(v => ({ ...v, body: e.target.value }))} />
                </div>
              </>
            )}

            {needMysql && (
              <>
                <div className="form-row">
                  <label>SQL</label>
                  <input type="text" value={p.mysql_query ?? ''} onChange={e => setP(v => ({ ...v, mysql_query: e.target.value }))} />
                </div>
                <div className="form-row">
                  <label>连接保持</label>
                  <input type="number" min={0} max={64} value={p.mysql_hold_connections ?? 0}
                         onChange={e => setP(v => ({ ...v, mysql_hold_connections: Number(e.target.value) }))} />
                  <span className="hint">每线程持住 N 条连接（打满 max_connections）</span>
                </div>
              </>
            )}
            <div className="form-note">
              目标只能从白名单里选（网关 / 副本 / MySQL）—— 这个接口刻意**不接受任意 URL**，
              否则内部运维台就变成了对任意主机打高并发的工具。
            </div>
          </section>
        </aside>

        <main className="stress-main">
          {result && <ResultCard s={result} />}

          <section className="sc-card grow">
            <h2>
              压测日志
              {job && <span className={`job-status ${job.status}`}>{job.status}</span>}
              {job?.step && <span className="hint">{job.step}</span>}
              {running && (
                <button className="stop-inline"
                        onClick={() => chaosApi.stopAndRollback().then(() => setBanner({ kind: 'warn', text: '已请求停止压测' }))}>
                  ⏹ 停止
                </button>
              )}
            </h2>
            {!job && <p className="empty">还没有压测任务。选好参数后点「▶ 开始压测」。</p>}
            {job && (
              <div className="log-box" ref={logBoxRef}>
                {logs.map((l, i) => (
                  <div className={`log-line ${l.level}`} key={i}>
                    <span className="log-t">{l.t.toFixed(1)}s</span>
                    <span className="log-m">{l.msg}</span>
                  </div>
                ))}
                {running && <div className="log-line running"><span className="log-m">…</span></div>}
              </div>
            )}
          </section>

          <section className="sc-card">
            <h2>历史压测（含文档基线，便于对比）</h2>
            {history.length === 0 && <p className="empty">暂无历史</p>}
            <div className="history-list">
              {history.map((h) => (
                <button key={h.source} className="history-row" onClick={() => setDetail(h)}>
                  <span className="hr-label">{h.label || h.mode}</span>
                  <span className="hr-meta">{h.mode} · 并发 {h.concurrency}</span>
                  <span className="hr-num">{(h.requests?.total ?? 0).toLocaleString()} 请求</span>
                  <span className={`hr-num ${h.requests?.error ? 'bad' : ''}`}>
                    {h.requests?.error ?? 0} 失败
                  </span>
                  <span className="hr-num">{fmt(h.throughput?.requests_per_sec)} rps</span>
                  <span className="hr-num">p99 {fmt(h.latency_ms?.p99)}ms</span>
                  <span className="hr-time">{h.started_at}</span>
                </button>
              ))}
            </div>
          </section>
        </main>
      </div>

      {pendingConfirm && (
        <div className="modal-backdrop" onClick={() => setPendingConfirm(null)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <h3>确认开始压测？</h3>
            <div className="modal-risk high">{pendingConfirm.impact}</div>
            <div className="modal-meta">
              <div>模式 <code>{p.mode}</code> · 并发 <code>{p.concurrency}</code> · 时长 <code>{p.duration}s</code></div>
              <div>压测期间集群负载会显著上升，建议不要同时做其他演示。</div>
            </div>
            <div className="modal-actions">
              <button onClick={() => setPendingConfirm(null)}>取消</button>
              <button className="danger" onClick={() => start(true)}>确认开始</button>
            </div>
          </div>
        </div>
      )}

      {detail && (
        <div className="modal-backdrop" onClick={() => setDetail(null)}>
          <div className="modal wide" onClick={e => e.stopPropagation()}>
            <h3>压测明细 · {detail.label || detail.mode}</h3>
            <ResultCard s={detail} />
            <div className="modal-actions">
              <button onClick={() => setDetail(null)}>关闭</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
