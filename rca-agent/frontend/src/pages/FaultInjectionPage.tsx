import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  chaosApi, getChaosToken, neutralPrompt, setChaosToken,
  type ClusterStatus, type DoctorResult, type EngineHealth,
  type Job, type JobLog, type Risk, type Scenario, type SignalRow,
} from '../api/chaos'
import './FaultInjectionPage.css'
import { runningJob } from '../state/runningJob'

const RISK_LABEL: Record<Risk, string> = {
  low: '低', medium: '中', high: '高', critical: '极高',
}

const LAYER_ORDER = ['application', 'runtime', 'database', 'resource']

interface Props {
  /** 切到诊断台并带上中性提示词（不泄露标准答案） */
  onDiagnose: (prompt: string) => void
}

export function FaultInjectionPage({ onDiagnose }: Props) {
  const [engine, setEngine] = useState<EngineHealth | null>(null)
  const [scenarios, setScenarios] = useState<Scenario[]>([])
  const [status, setStatus] = useState<ClusterStatus | null>(null)
  const [doctorRes, setDoctorRes] = useState<DoctorResult | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const [logs, setLogs] = useState<JobLog[]>([])
  const [busy, setBusy] = useState(false)
  const [banner, setBanner] = useState<{ kind: 'info' | 'error' | 'warn'; text: string } | null>(null)
  //: 故障注入接口令牌（仅存浏览器本地；后端设了 CHAOS_API_TOKEN 时必须填）
  const [token, setToken] = useState(() => getChaosToken())
  const [layerFilter, setLayerFilter] = useState<string>('all')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [paramDraft, setParamDraft] = useState<Record<string, string>>({})
  const [preview, setPreview] = useState<Record<string, string[]>>({})
  const [signalMap, setSignalMap] = useState<Record<string, SignalRow[]>>({})
  const [pendingConfirm, setPendingConfirm] = useState<Scenario | null>(null)
  const [confirmVerify, setConfirmVerify] = useState(false)
  //: 批量演练是否跑全部 21 个场景（默认关：只跑代表性子集，误点代价小得多）
  const [fullVerify, setFullVerify] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [tick, setTick] = useState(0)

  const logBoxRef = useRef<HTMLDivElement>(null)
  const logOffsetRef = useRef(0)
  const pollRef = useRef<number | null>(null)
  /** 当前正在轮询的任务 id（用于判断"服务器上的任务是不是我已经在跟的那个"） */
  const jobIdRef = useRef<string>('')

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await chaosApi.status())
    } catch {
      /* 静默：状态轮询失败不打扰用户 */
    }
  }, [])

  // ── 初始加载 ────────────────────────────────────────────────────
  useEffect(() => {
    ;(async () => {
      try {
        const h = await chaosApi.health()
        setEngine(h)
        if (!h.available) {
          setBanner({ kind: 'error', text: h.reason || '注入引擎不可用' })
          return
        }
        const s = await chaosApi.scenarios()
        setScenarios(s.scenarios)
        await refreshStatus()
        // 恢复最近一次任务：刷新页面 / 换设备后仍能看到上次做了什么、日志是什么。
        // ⚠️ 如果那个任务还在跑，必须**接管它**（设置 busy 并继续轮询）——
        // 否则"演练正在运行但页面上看不到停止按钮"，用户就再也停不下来了。
        const recent = await chaosApi.jobs(1)
        const last = recent.jobs[0]
        if (last) {
          const full = await chaosApi.job(last.id, 0)
          setJob(full)
          setLogs(full.logs ?? [])
          logOffsetRef.current = full.log_offset ?? (full.logs?.length ?? 0)
          if (full.status === 'running' || full.status === 'queued') {
            setBusy(true)
            // 接管的同时登记到共享状态：这样用户切到别的页面也能看到"演练还在跑"
            runningJob.start({ id: full.id, kind: 'chaos',
                               label: full.fault_id || full.kind || '演练' })
            pollJob(full.id)
          }
        }
      } catch (e) {
        setBanner({ kind: 'error', text: String((e as Error).message) })
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── 状态轮询（5s）：集群状态 + 接管"别处启动的"任务 ──────────────
  useEffect(() => {
    const id = window.setInterval(async () => {
      refreshStatus()
      setTick(t => t + 1)
      // 服务器上有任务在跑、而本页面没有在跟它（可能是另一个标签页/刷新前启动的）
      // → 主动接管，保证「停止并回滚」按钮任何时候都能看到。
      if (pollRef.current) return
      try {
        const js = await chaosApi.jobs(1)
        const newest = js.jobs[0]
        if (newest && (newest.status === 'running' || newest.status === 'queued')
            && newest.id !== jobIdRef.current) {
          setBusy(true)
          runningJob.start({ id: newest.id, kind: 'chaos',
                             label: newest.fault_id || newest.kind || '演练' })
          pollJob(newest.id)
        }
      } catch { /* ignore */ }
    }, 5000)
    return () => window.clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 有激活故障时自动展开它的卡片：注入后立刻能看到参数与信号灯，
  // 不用再手动点开（人工演练里最常看的就是这一屏）。
  useEffect(() => {
    const act = status?.active_faults ?? []
    if (act.length === 1 && !expanded && scenarios.length) {
      const sc = scenarios.find(s => s.id === act[0].fault_id)
      if (sc) void toggleExpand(sc)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, scenarios])

  // ── 任务日志轮询（1s，增量拉取）─────────────────────────────────
  const pollJob = useCallback((jobId: string) => {
    if (pollRef.current) window.clearInterval(pollRef.current)
    jobIdRef.current = jobId
    logOffsetRef.current = 0
    setLogs([])
    pollRef.current = window.setInterval(async () => {
      try {
        const j = await chaosApi.job(jobId, logOffsetRef.current)
        setJob(j)
        if (j.logs && j.logs.length) {
          logOffsetRef.current = j.log_offset ?? logOffsetRef.current + j.logs.length
          setLogs(prev => [...prev, ...j.logs!])
        }
        const done = j.status === 'done' || j.status === 'failed' || j.status === 'cancelled'
        setBusy(!done)
        if (done) {
          if (pollRef.current) window.clearInterval(pollRef.current)
          pollRef.current = null
          runningJob.finish(jobId)      // 结束即摘掉全局"运行中"指示
          await refreshStatus()
          if (j.kind === 'doctor') {
            setDoctorRes(j.result as unknown as DoctorResult)
          }
          if (j.status === 'cancelled') {
            setBanner({
              kind: 'warn',
              text: `已停止并回滚${j.result?.stopped_at ? `（${j.result.stopped_at}）` : ''}：残留已清理，环境已复原`,
            })
          } else if (j.status === 'failed') {
            setBanner({ kind: 'error', text: j.error || '任务失败，详见左侧日志' })
          } else if (j.kind === 'inject') {
            setBanner({ kind: 'info', text: `注入完成：${j.fault_id}` })
          } else if (j.kind === 'recover' || j.kind === 'recover_all') {
            setBanner({ kind: 'info', text: '回滚完成' })
          } else if (j.kind === 'cleanup') {
            setBanner({ kind: 'info', text: '紧急清理完成' })
          } else if (j.kind === 'verify') {
            setBanner({ kind: 'info', text: `批量演练：${j.result?.passed}/${j.result?.attempted ?? j.result?.total} 通过` })
          }
        }
      } catch (e) {
        if (pollRef.current) window.clearInterval(pollRef.current)
        pollRef.current = null
        setBusy(false)
        runningJob.finish(jobId)
        setBanner({ kind: 'error', text: String((e as Error).message) })
      }
    }, 1000)
  }, [refreshStatus])

  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current) }, [])

  // 日志自动滚动到底
  useEffect(() => {
    const el = logBoxRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [logs.length])

  // ── 动作 ────────────────────────────────────────────────────────
  const submitJob = useCallback(async (p: Promise<{ ok: boolean; job_id?: string; error?: string }>) => {
    setBanner(null)
    try {
      const r = await p
      if (!r.ok || !r.job_id) {
        setBanner({ kind: 'warn', text: r.error || '提交失败' })
        return
      }
      setBusy(true)
      setJob(null)
      // 先登记再轮询：启动瞬间切走也能在别的页面看到它（批量演练常跑 20–40 分钟）
      runningJob.start({ id: r.job_id, kind: 'chaos', label: '演练任务' })
      pollJob(r.job_id)
    } catch (e) {
      const err = e as Error & { body?: { error?: string; impact?: string; need_confirm?: boolean } }
      setBanner({ kind: 'error', text: err.body?.error || err.message })
    }
  }, [pollJob])

  const doInject = useCallback(async (sc: Scenario, confirm = false) => {
    const params: Record<string, unknown> = {}
    sc.params_spec.forEach(p => {
      const raw = paramDraft[`${sc.id}.${p.name}`]
      if (raw !== undefined && raw !== '') params[p.name] = p.type === 'int' ? Number(raw) : raw
    })
    // 把预览到的目标一起提交 →「预览即实际目标」（roundrobin 场景必需）
    let targets = preview[sc.id]
    if (!targets) {
      try {
        const p = await chaosApi.preview(sc.id)
        if (p.ok) {
          targets = p.targets
          setPreview(prev => ({ ...prev, [sc.id]: p.targets }))
        }
      } catch { /* 预览失败则让后端按 selector 解析 */ }
    }
    await submitJob(chaosApi.inject(sc.id, params, confirm, targets ?? []))
    setPendingConfirm(null)
  }, [paramDraft, preview, submitJob])

  const onInjectClick = useCallback(async (sc: Scenario) => {
    // 先预览目标，让操作者知道"会打到谁"
    try {
      const p = await chaosApi.preview(sc.id)
      if (p.ok) setPreview(prev => ({ ...prev, [sc.id]: p.targets }))
    } catch { /* 预览失败不阻断 */ }

    if (sc.risk === 'high' || sc.risk === 'critical') {
      setPendingConfirm(sc)
      return
    }
    await doInject(sc, false)
  }, [doInject])

  const toggleExpand = useCallback(async (sc: Scenario) => {
    const next = expanded === sc.id ? null : sc.id
    setExpanded(next)
    if (next) {
      if (!preview[sc.id]) {
        try {
          const p = await chaosApi.preview(sc.id)
          if (p.ok) setPreview(prev => ({ ...prev, [sc.id]: p.targets }))
        } catch { /* ignore */ }
      }
      try {
        const s = await chaosApi.signals(sc.id)
        if (s.ok) setSignalMap(prev => ({ ...prev, [sc.id]: s.signals }))
      } catch { /* ignore */ }
    }
  }, [expanded, preview])

  const refreshSignals = useCallback(async (sc: Scenario) => {
    try {
      const s = await chaosApi.signals(sc.id)
      if (s.ok) setSignalMap(prev => ({ ...prev, [sc.id]: s.signals }))
    } catch { /* ignore */ }
  }, [])

  // ── 停止并强制回滚（误点演练的紧急出口）─────────────────────────
  const stopAndRollback = useCallback(async () => {
    setStopping(true)
    setBanner({ kind: 'warn', text: '已请求停止：当前步骤的取消点到达后会立即回滚（≤5s）…' })
    try {
      const r = await chaosApi.stopAndRollback()
      setBanner({ kind: 'warn', text: r.note })
      // 任务结束后会自动进入收尾清理；继续轮询就能看到日志
      if (job?.id) pollJob(job.id)
      else {
        const recent = await chaosApi.jobs(1)
        if (recent.jobs[0]) pollJob(recent.jobs[0].id)
      }
    } catch (e) {
      setBanner({ kind: 'error', text: String((e as Error).message) })
    } finally {
      window.setTimeout(() => setStopping(false), 4000)
    }
  }, [job?.id, pollJob])

  const startVerify = useCallback(async () => {
    setConfirmVerify(false)
    // 默认只跑代表性子集（后端在 fault_ids 为空时给出），勾选后才跑全部 21 个。
    await submitJob(chaosApi.verify({ hold: 20, full: fullVerify }))
  }, [submitJob])

  // ── 派生数据 ────────────────────────────────────────────────────
  const byLayer = useMemo(() => {
    const m: Record<string, Scenario[]> = {}
    scenarios.forEach(s => {
      const key = s.layer === 'runtime' ? 'application' : s.layer
      ;(m[key] = m[key] || []).push(s)
    })
    return m
  }, [scenarios])

  const visible = useMemo(() => {
    if (layerFilter === 'all') return scenarios
    return scenarios.filter(s => (s.layer === 'runtime' ? 'application' : s.layer) === layerFilter)
  }, [scenarios, layerFilter])

  const activeFaults = status?.active_faults ?? []
  const activeIds = new Set(activeFaults.map(a => a.fault_id))
  /** 面板是否应呈现"运行中"：既看本页提交的任务，也看服务器上真实在跑的任务。
   *  只看本地 busy 会导致"演练在跑但看不到停止按钮"（刷新/换标签页后必现）。 */
  const running = busy || (job != null && (job.status === 'running' || job.status === 'queued'))

  const elapsed = (iso?: string): string => {
    if (!iso) return '-'
    const t = Date.parse(iso)
    if (Number.isNaN(t)) return iso
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000))
    if (s < 60) return `${s}s`
    if (s < 3600) return `${Math.floor(s / 60)}m${s % 60}s`
    return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`
  }

  const replicasOk = (status?.app_replicas ?? []).filter(r => r.state === 'running').length
  const promUp = status?.prometheus_up

  return (
    <div className="chaos-page">
      {/* ── 顶部：环境状态 + 全局动作 ───────────────────────────── */}
      <header className="chaos-header">
        <div className="chaos-title">
          <span className="chaos-logo">⚡</span>
          <h1>故障注入控制台</h1>
          <span className="chaos-sub">人工手动注入 · 诊断只读 / 注入只写</span>
        </div>

        <div className="chaos-health">
          <span className={`dot ${replicasOk >= 3 ? 'ok' : 'bad'}`} />
          <span>副本 {replicasOk}/3</span>
          <span className={`dot ${Object.keys(status?.mysql ?? {}).length >= 3 ? 'ok' : 'bad'}`} />
          <span>MySQL {Object.keys(status?.mysql ?? {}).length}/3</span>
          <span className={`dot ${promUp && promUp.up > 0 ? 'ok' : 'bad'}`} />
          <span>Prom {promUp ? `${promUp.up}/${promUp.total}` : '-'}</span>
          {doctorRes && (
            <>
              <span className={`dot ${doctorRes.passed ? 'ok' : 'bad'}`} />
              <span>体检 {doctorRes.ok_count}/{doctorRes.total}</span>
            </>
          )}
        </div>

        <div className="chaos-actions">
          {/* 访问令牌：后端设了 CHAOS_API_TOKEN 时必填（对外暴露前必须设）。
              存在浏览器本地、不会发给 Agent。 */}
          <button className={token ? 'token-set' : ''}
                  onClick={() => {
                    const v = window.prompt(
                      '故障注入接口令牌（对应后端 CHAOS_API_TOKEN）\n' +
                      '留空 = 清除。仅存本机浏览器，不会发给智能体。', token)
                    if (v !== null) { setChaosToken(v.trim()); setToken(v.trim()) }
                  }}
                  title={token ? '已设置令牌（点击修改/清除）'
                               : '未设置令牌；后端若设了 CHAOS_API_TOKEN 则必须填写'}>
            🔑 {token ? '令牌已设' : '令牌'}
          </button>
          {running ? (
            <button className="stop" onClick={stopAndRollback} disabled={stopping}
                    title="停止当前任务（含批量演练）并强制回滚 + 清理残留">
              {stopping ? '⏳ 正在停止…' : '⏹ 停止并回滚'}
            </button>
          ) : (
            <button onClick={() => setConfirmVerify(true)}
                    title="批量自动演练：注入→等信号→保持→回滚→校验恢复（默认 7 个代表性子集，约 10–15 分钟；确认框里可改为全部 21 个）">
              🤖 批量演练
            </button>
          )}
          <button onClick={async () => setDoctorRes(await chaosApi.doctor())} disabled={running}
                  title="环境体检：容器/网关/读路径/MySQL/复制/Prometheus/告警规则/体量">
            🩺 环境体检
          </button>
          <button onClick={() => submitJob(chaosApi.recover(undefined, true))}
                  disabled={running || activeFaults.length === 0}
                  title="按 .chaos/fault_state.json 回滚全部激活故障">
            ↩ 全部回滚
          </button>
          <button className="danger" disabled={running}
                  onClick={() => {
                    if (window.confirm('紧急清理会不依赖状态文件，强制清除容器内所有注入痕迹（压测进程、注入循环、残留会话、被改过的全局变量、只读配置、cgroup 内存上限）。继续？')) {
                      submitJob(chaosApi.cleanup())
                    }
                  }}
                  title="兜底清理：不依赖状态文件，进程被杀也能复原">
            🚨 紧急清理
          </button>
        </div>
      </header>

      {banner && (
        <div className={`chaos-banner ${banner.kind}`}>
          {banner.text}
          <button className="link" onClick={() => setBanner(null)}>关闭</button>
        </div>
      )}

      <div className="chaos-body">
        {/* ── 左栏：激活故障 + 任务日志 ─────────────────────────── */}
        <aside className="chaos-side">
          <section className="chaos-card">
            <h2>
              当前激活故障 <span className="count">{activeFaults.length}</span>
              {(status?.stale_count ?? 0) > 0 && (
                <span className="stale-badge" title="记录仍在状态文件里，但声明信号全部不成立">
                  {status?.stale_count} 条疑似残留
                </span>
              )}
            </h2>
            {activeFaults.length === 0 ? (
              <p className="empty">环境干净：无激活故障</p>
            ) : (
              activeFaults.map((a, i) => (
                <div className="active-fault" key={`${a.fault_id}-${i}`}>
                  <div className="af-head">
                    <strong>{a.title || a.fault_id}</strong>
                    <span className={`risk ${scenarios.find(s => s.id === a.fault_id)?.risk ?? 'medium'}`}>
                      {RISK_LABEL[(scenarios.find(s => s.id === a.fault_id)?.risk ?? 'medium') as Risk]}
                    </span>
                  </div>
                  <div className="af-meta">
                    <code>{a.fault_id}</code>
                    <span>已持续 {elapsed(a.injected_at)}</span>
                  </div>
                  <div className="af-meta">
                    <span>目标：{(a.targets ?? []).join(', ') || '-'}</span>
                  </div>
                  {/* 记录还在、但声明信号全不成立 → 提示这是残留记录，别被它骗了 */}
                  {a.signal_state && a.signal_state.state !== 'active' && (
                    <div className={`af-signal ${a.signal_state.state}`}>
                      {a.signal_state.state === 'quiet' && '⚠ 疑似残留记录：'}
                      {a.signal_state.state === 'quiet_under_load' && 'ℹ 需压测才可观测：'}
                      {a.signal_state.state === 'unknown' && 'ℹ 信号无法判定：'}
                      {a.signal_state.reason}
                    </div>
                  )}
                  {a.params && Object.keys(a.params).length > 0 && (
                    <div className="af-meta">
                      <span>参数：{JSON.stringify(a.params)}</span>
                    </div>
                  )}
                  <div className="af-actions">
                    <button className="primary" disabled={running}
                            onClick={() => submitJob(chaosApi.recover(a.fault_id))}>
                      ↩ 回滚此故障
                    </button>
                    <button disabled={running} onClick={() => {
                      const sc = scenarios.find(s => s.id === a.fault_id)
                      if (sc) onDiagnose(neutralPrompt(sc, a.targets ?? []))
                    }}>🔍 到诊断台</button>
                  </div>
                </div>
              ))
            )}
          </section>

          <section className="chaos-card grow">
            <h2>
              任务日志
              {job && <span className={`job-status ${job.status}`}>{job.status}</span>}
              {running && (
                <button className="stop-inline" onClick={stopAndRollback} disabled={stopping}>
                  ⏹ 停止并回滚
                </button>
              )}
              {job?.cancel_requested && job.status === 'running' && (
                <span className="stopping-hint">停止中…</span>
              )}
            </h2>
            {!job && <p className="empty">还没有任务。点任意场景的「注入」开始。</p>}
            {job && (
              <>
                <div className="job-meta">
                  <code>{job.kind}{job.fault_id && job.fault_id !== 'all' ? ` · ${job.fault_id}` : ''}</code>
                  <span>{job.step}</span>
                  <span>{job.duration_s}s</span>
                </div>
                <div className="log-box" ref={logBoxRef}>
                  {logs.map((l, i) => (
                    <div className={`log-line ${l.level}`} key={i}>
                      <span className="log-t">{l.t.toFixed(1)}s</span>
                      <span className="log-m">{l.msg}</span>
                    </div>
                  ))}
                  {running && <div className="log-line running"><span className="log-m">…</span></div>}
                </div>
              </>
            )}
          </section>

          {doctorRes && (
            <section className="chaos-card">
              <h2>环境体检 <span className="count">{doctorRes.ok_count}/{doctorRes.total}</span></h2>
              <div className="doctor-list">
                {doctorRes.checks.map(c => (
                  <div className={`doctor-row ${c.ok ? 'ok' : 'bad'}`} key={c.name}>
                    <span>{c.ok ? '✅' : '❌'}</span>
                    <span className="dr-name">{c.name}</span>
                    <span className="dr-detail">{String(c.detail ?? '')}</span>
                  </div>
                ))}
              </div>
            </section>
          )}
        </aside>

        {/* ── 主区：场景目录 ───────────────────────────────────── */}
        <main className="chaos-main">
          <div className="chaos-tabs">
            <button className={layerFilter === 'all' ? 'on' : ''} onClick={() => setLayerFilter('all')}>
              全部 <b>{scenarios.length}</b>
            </button>
            {LAYER_ORDER.filter(l => byLayer[l]?.length).map(l => (
              <button key={l} className={layerFilter === l ? 'on' : ''} onClick={() => setLayerFilter(l)}>
                {l === 'application' ? '应用层集群' : l === 'database' ? '数据库层集群' : '资源耗尽'}
                <b>{byLayer[l].length}</b>
              </button>
            ))}
            <span className="chaos-hint">
              点卡片展开参数与信号灯 · 高/极高危险场景需要二次确认
            </span>
          </div>

          <div className="scenario-grid">
            {visible.map(sc => {
              const isActive = activeIds.has(sc.id)
              const isOpen = expanded === sc.id
              const sigs = signalMap[sc.id]
              const injSig = (sigs ?? []).filter(s => s.kind === 'inject')
              const anyTruthy = injSig.some(s => s.truthy)
              return (
                <div className={`scenario-card ${isActive ? 'active' : ''} ${isOpen ? 'open' : ''}`} key={sc.id}>
                  <div className="sc-head" onClick={() => toggleExpand(sc)}>
                    <div className="sc-title-row">
                      <span className={`risk ${sc.risk}`}>{RISK_LABEL[sc.risk]}</span>
                      <strong>{sc.title}</strong>
                      {isActive && <span className="badge-active">已注入</span>}
                    </div>
                    <code className="sc-id">{sc.id}</code>
                  </div>

                  <p className="sc-desc">{sc.description}</p>

                  <div className="sc-tags">
                    <span className="tag">{sc.layer_label}</span>
                    <span className="tag">期望类别 {sc.category}</span>
                    {sc.needs_stress && <span className="tag warn">需叠加压测</span>}
                    {sc.alert_names.slice(0, 2).map(a => <span className="tag alert" key={a}>{a}</span>)}
                  </div>

                  <div className="sc-impact">影响面：{sc.impact}</div>

                  {isOpen && (
                    <div className="sc-detail">
                      <div className="sc-row">
                        <label>目标容器（selector={sc.selector}）</label>
                        <div className="targets">
                          {(preview[sc.id] ?? []).length
                            ? preview[sc.id].map(t => <code key={t}>{t}</code>)
                            : <span className="muted">点「注入」时实时解析</span>}
                        </div>
                      </div>

                      <div className="sc-row">
                        <label>标准答案（仅供人工对照，<b>不会发送给智能体</b>）</label>
                        <div className="answer">{sc.expected_root_cause}</div>
                        <div className="onto-terms">
                          {sc.ontology_terms.map(t => <code key={t}>{t}</code>)}
                        </div>
                      </div>

                      {sc.params_spec.length > 0 && (
                        <div className="sc-row">
                          <label>注入参数</label>
                          <div className="params">
                            {sc.params_spec.map(p => (
                              <div className="param" key={p.name}>
                                <span className="p-name">{p.name}</span>
                                <input
                                  type={p.type === 'int' ? 'number' : 'text'}
                                  value={paramDraft[`${sc.id}.${p.name}`] ?? String(p.default ?? '')}
                                  onChange={e => setParamDraft(prev => ({ ...prev, [`${sc.id}.${p.name}`]: e.target.value }))}
                                />
                                <span className="p-help">{p.help}</span>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}

                      <div className="sc-row">
                        <label>
                          信号灯（PromQL 实时求值）
                          <button className="link" onClick={(e) => { e.stopPropagation(); refreshSignals(sc) }}>
                            刷新
                          </button>
                        </label>
                        <div className="signals">
                          {(sigs ?? []).length === 0 && <span className="muted">展开后自动求值…</span>}
                          {(sigs ?? []).map((s, i) => (
                            <div className={`sig ${s.truthy ? 'truthy' : 'falsy'}`} key={i}>
                              <span className="sig-dot" />
                              <span className="sig-kind">{s.kind === 'inject' ? '注入' : '恢复'}</span>
                              <code>{s.expr}</code>
                              <span className="sig-val">{s.value === null ? 'no data' : s.value.toFixed(3)}</span>
                            </div>
                          ))}
                          {injSig.length > 0 && (
                            <div className={`sig-verdict ${anyTruthy ? 'ok' : ''}`}>
                              {anyTruthy
                                ? '注入信号已成立 → 故障真实生效'
                                : '注入信号全为假 → 尚未生效（可能需叠加压测或等待采集）'}
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                  )}

                  <div className="sc-actions">
                    <button className="primary" disabled={running || isActive} onClick={() => onInjectClick(sc)}>
                      {isActive ? '已注入' : '⚡ 注入'}
                    </button>
                    <button disabled={running || !isActive} onClick={() => submitJob(chaosApi.recover(sc.id))}>
                      ↩ 回滚
                    </button>
                    <button className="link" onClick={() => toggleExpand(sc)}>
                      {isOpen ? '收起' : '详情'}
                    </button>
                    {isActive && (
                      <button className="link" onClick={() => onDiagnose(neutralPrompt(sc, preview[sc.id] ?? []))}>
                        到诊断台 →
                      </button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </main>
      </div>

      {/* ── 批量演练二次确认 ── */}
      {confirmVerify && (
        <div className="modal-backdrop" onClick={() => setConfirmVerify(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <h3>开始批量演练？</h3>
            <div className="modal-risk high">
              默认只跑<b>代表性子集</b>（7 个场景，覆盖应用层/数据库层/跨层资源，
              约 10–15 分钟）。勾选下面的选项才会依次注入<b>全部 21 个场景</b>，
              那需要 <b>20–40 分钟</b>。
            </div>
            <div className="modal-meta">
              <div>期间集群会反复处于故障状态，不适合同时做人工演示或诊断对比。</div>
              <div>
                随时可以点顶部或日志面板里的 <b>「⏹ 停止并回滚」</b> 中止 ——
                停止后会自动回滚当前故障并清理全部残留，不需要别的操作。
              </div>
              <label className="verify-full">
                <input type="checkbox" checked={fullVerify}
                       onChange={e => setFullVerify(e.target.checked)} />
                跑全部 21 个场景（破坏性更强、耗时 20–40 分钟）
              </label>
            </div>
            <div className="modal-actions">
              <button onClick={() => setConfirmVerify(false)}>取消</button>
              <button className="danger" onClick={startVerify}>确认开始演练</button>
            </div>
          </div>
        </div>
      )}

      {/* ── 高危险场景二次确认 ─────────────────────────────────── */}
      {pendingConfirm && (
        <div className="modal-backdrop" onClick={() => setPendingConfirm(null)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <h3>确认注入：{pendingConfirm.title}</h3>
            <div className={`modal-risk ${pendingConfirm.risk}`}>
              危险等级 <b>{RISK_LABEL[pendingConfirm.risk]}</b> · {pendingConfirm.impact}
            </div>
            <p>{pendingConfirm.description}</p>
            <div className="modal-meta">
              <div>场景 <code>{pendingConfirm.id}</code></div>
              <div>期望根因（标准答案，不发送给智能体）：{pendingConfirm.expected_root_cause}</div>
              <div>目标：{(preview[pendingConfirm.id] ?? []).join(', ') || '注入时解析'}</div>
            </div>
            <div className="modal-actions">
              <button onClick={() => setPendingConfirm(null)}>取消</button>
              <button className="danger" onClick={() => doInject(pendingConfirm, true)}>
                确认注入
              </button>
            </div>
          </div>
        </div>
      )}

      <footer className="chaos-foot">
        <span>
          引擎：{engine?.available ? '就绪' : '不可用'}
          {engine && ` · 场景 ${engine.scenarios}`}
          {engine?.docker_cli ? ` · docker ${engine.docker_cli}` : ''}
          {engine?.token_required ? ' · 已启用令牌' : ''}
        </span>
        <span className="tick">刷新 {tick > 0 ? `${tick * 5}s` : '—'}</span>
      </footer>
    </div>
  )
}
