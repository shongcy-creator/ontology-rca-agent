import { useEffect, useState } from 'react'
import './MetricsPanel.css'

interface Evidence {
  http_latency?: Record<string, unknown>
  mysql_pool?: Record<string, string>
  mysql_global?: Record<string, string>
  txn?: Record<string, string>
  /** 后端已按 le 聚合：{ buckets: {le: 累计计数}, total: +Inf 总数 } */
  http_latency_buckets?: { buckets?: Record<string, number>; total?: number } | Record<string, number>
  targets?: Array<{ job: string; health: string; lastError?: string }>
}

interface Props { evidence: Evidence | null }

/** Prometheus 即时查询返回的一条 series */
interface PromSeries {
  metric?: Record<string, string>
  /** ⚠ 不是标量：`[样本时间戳, "值字符串"]` */
  value?: [number, string] | string | number
}

/**
 * HTTP 延迟的合理上限（秒）。超过这个值基本可以断定是**解析或查询**出了问题，
 * 而不是真的有请求跑了 2 分钟 —— 宁可显示"数据异常"，也不要展示一个荒谬但
 * 看起来权威的数字（曾经把样本时间戳当延迟显示成 17.9 亿秒）。
 */
const SANE_LATENCY_MAX_S = 120

/**
 * 从 Prometheus 样本里取出**数值**。
 *
 * 关键点：`value` 是 `[时间戳, "字符串值"]`，不是一个标量。
 * 直接 `parseFloat(series.value)` 会把数组转成 "1791002517.868,0.0895"
 * 然后取到**时间戳**（这就是 17.9 亿秒那个数字的来源）。
 * 这里只取下标 1，并把 NaN/±Inf/空值统一收敛成 null。
 */
function promScalar(v: unknown): number | null {
  let raw: unknown = v
  if (Array.isArray(v)) raw = v.length > 1 ? v[1] : v[0]
  if (raw === null || raw === undefined) return null
  const s = String(raw).trim()
  if (s === '' || s === 'NaN' || s === '+Inf' || s === '-Inf' || s === 'Inf') return null
  const n = Number(s)
  return Number.isFinite(n) ? n : null
}

/** 一条 series 的可读来源标签（用于说明"最差值来自哪个副本/路径"） */
function seriesLabel(s: PromSeries): string {
  const m = s.metric || {}
  const parts: string[] = []
  if (m.path) parts.push(m.path)
  if (m.instance) parts.push(m.instance)
  else if (m.app_instance) parts.push(m.app_instance)
  return parts.join(' @ ') || '—'
}

/**
 * 把一个百分位的多条 series 汇总成"最差值 + 来源"。
 *
 * 为什么取最差而不是第一条：`histogram_quantile` 会按 `app_instance × path`
 * 展开成多条 series，`[0]` 只是任意一条（实测那一条恰好是 NaN）。
 * 延迟面板应当看**最差**的那条，并标出来源。
 */
function summarizeLatency(series: unknown): {
  worst: number | null; where: string; total: number; nanCount: number
} {
  const arr = Array.isArray(series) ? (series as PromSeries[]) : []
  let worst: number | null = null
  let where = '—'
  let nanCount = 0
  for (const s of arr) {
    const v = promScalar(s?.value)
    if (v === null) { nanCount += 1; continue }
    if (worst === null || v > worst) { worst = v; where = seriesLabel(s) }
  }
  return { worst, where, total: arr.length, nanCount }
}

export function MetricsPanel({ evidence }: Props) {
  const [live, setLive] = useState<Evidence | null>(null)

  // 首次加载时从后端拉取实时数据
  useEffect(() => {
    fetch('/api/metrics/summary')
      .then(r => r.json())
      .then(setLive)
      .catch(() => {/* ignore */})
  }, [])

  const data = live || evidence
  if (!data) {
    return (
      <div className="metrics-empty">
        <p>📈 暂无监控数据</p>
        <p className="hint">执行 RCA 分析后，Prometheus 指标将显示在此处</p>
        <button onClick={() => fetch('/api/metrics/summary').then(r => r.json()).then(setLive)}>
          刷新
        </button>
      </div>
    )
  }

  // 兼容两种返回：新版 {buckets,total}，旧版直接是 le→count 的字典
  const rawBuckets = data.http_latency_buckets
  const buckets: Record<string, number> = (rawBuckets && typeof rawBuckets === 'object'
    && 'buckets' in rawBuckets)
    ? ((rawBuckets as { buckets?: Record<string, number> }).buckets || {})
    : ((rawBuckets as Record<string, number>) || {})
  // 累积直方图的分母是 **+Inf 总数**，不是各桶相加（相加会把同一个请求算多次）
  const total = (rawBuckets && typeof rawBuckets === 'object' && 'total' in rawBuckets
    ? Number((rawBuckets as { total?: number }).total) || 0
    : Object.values(buckets).reduce((s, v) => s + Number(v || 0), 0))

  return (
    <div className="metrics-panel">
      {/* Targets */}
      {data.targets && data.targets.length > 0 && (
        <div className="metrics-section">
          <div className="metrics-title">🎯 Prometheus Targets</div>
          {data.targets.map((t, i) => (
            <div key={i} className={`target-row ${t.health === 'up' ? 'up' : 'down'}`}>
              <span className="t-dot" />
              <span className="t-job">{t.job}</span>
              <span className={`t-status ${t.health}`}>{t.health}</span>
            </div>
          ))}
        </div>
      )}

      {/* MySQL Pool */}
      {data.mysql_pool && (
        <div className="metrics-section">
          <div className="metrics-title">
            🧵 MySQL 连接池
            {data.mysql_pool.replicas && (
              <span className="metrics-sub">（{data.mysql_pool.replicas} 副本合计）</span>
            )}
            {/* 指标就绪度：懒创建的池指标在无流量副本上不存在，
                只显示 0 会被读成"真的没有连接"（见验证手册 §11.21 第 4 项）。 */}
            {data.mysql_pool.metrics_ok !== undefined && (
              <span className={`metrics-sub${Number(data.mysql_pool.metrics_ok) <
                Number(data.mysql_pool.replicas) ? ' warn' : ''}`}>
                · 池指标就绪 {data.mysql_pool.metrics_ok}/{data.mysql_pool.replicas} 副本
              </span>
            )}
          </div>
          <div className="metric-grid">
            <MetricCard label="活跃连接" value={data.mysql_pool.active} unit=""
                        title="各副本 cc_mysql_pool_active 之和" />
            <MetricCard label="空闲连接" value={data.mysql_pool.idle} unit=""
                        title="各副本 cc_mysql_pool_idle 之和" />
            <MetricCard label="连接上限" value={data.mysql_pool.limit} unit=""
                        warn={data.mysql_pool.limit_mismatch === '1'}
                        title={data.mysql_pool.limit_mismatch === '1'
                          ? `各副本上报的 limit 不一致（${data.mysql_pool.limit_values}）—— 这是数据源问题，不是真的没配额`
                          : '各副本 cc_mysql_pool_limit 的最大值（配置值）'} />
          </div>
          {data.mysql_pool.limit_mismatch === '1' && (
            <div className="metrics-hint warn">
              ⚠ 各副本上报的连接上限不一致：{data.mysql_pool.limit_values}
              —— 说明有副本自身这个指标就是错的（面板取最大值展示），
              排查时别把它当成"配额真的是 0"。
            </div>
          )}
          {data.mysql_pool.pool_all_zero === '1' && (
            <div className="metrics-hint warn">
              ⚠ 所有副本的活跃/空闲连接都是 0，且只有 {data.mysql_pool.metrics_ok}/
              {data.mysql_pool.replicas} 个副本在暴露池指标：
              <strong>这是"池指标还没被创建"而不是"集群真的没有连接"</strong>
              （池是懒创建的，节点上没流量就不建池）。判断读路径是否健康请看
              「副本可达性」，不要看这里的 0。
            </div>
          )}
        </div>
      )}

      {/* MySQL Global */}
      {data.mysql_global && (
        <div className="metrics-section">
          <div className="metrics-title">🗄 MySQL 全局状态</div>
          <div className="metric-grid">
            <MetricCard label="threads_connected" value={data.mysql_global.threads_connected} unit="" />
            <MetricCard label="max_used_conn" value={data.mysql_global.max_used_connections} unit="" />
            <MetricCard label="uptime" value={data.mysql_global.uptime} unit="s" />
          </div>
        </div>
      )}

      {/* HTTP Latency */}
      {data.http_latency && (
        <div className="metrics-section">
          <div className="metrics-title">🌐 HTTP 延迟（histogram_quantile · 5m）</div>
          <div className="metric-grid">
            {Object.entries(data.http_latency).map(([key, series]) => {
              const { worst, where, total, nanCount } = summarizeLatency(series)
              if (worst === null) {
                return (
                  <MetricCard key={key} label={key.toUpperCase()} value="无数据"
                              unit="" title={`${total} 条 series 全部为 NaN/空`} />
                )
              }
              const absurd = worst > SANE_LATENCY_MAX_S
              return (
                <MetricCard
                  key={key}
                  label={key.toUpperCase()}
                  value={absurd ? '数据异常' : worst.toFixed(3)}
                  unit={absurd ? '' : 's'}
                  title={absurd
                    ? `解析出的值为 ${worst}，超过合理上限 ${SANE_LATENCY_MAX_S}s，请检查 PromQL 与取值逻辑`
                    : `最差来源：${where}（共 ${total} 条 series${nanCount ? `，${nanCount} 条 NaN 已忽略` : ''}）`}
                  warn={absurd}
                />
              )
            })}
          </div>
          {(() => {
            // 说明"最差来自哪里"，避免面板只给一个孤零零的数字
            const rows = Object.entries(data.http_latency)
              .map(([k, s]) => ({ k, ...summarizeLatency(s) }))
              .filter(r => r.worst !== null)
            if (rows.length === 0) return null
            const top = rows.reduce((a, b) => ((b.worst ?? 0) > (a.worst ?? 0) ? b : a))
            return (
              <div className="metrics-hint">
                最差 {top.k.toUpperCase()} = {(top.worst as number).toFixed(3)}s，来源 {top.where}
                （取自 {top.total} 条 series；NaN/无数据已忽略）
              </div>
            )
          })()}
        </div>
      )}

      {/* HTTP Latency Buckets */}
      {buckets && Object.keys(buckets).length > 0 && (
        <div className="metrics-section">
          <div className="metrics-title">
            📊 HTTP 延迟分布
            <span className="metrics-sub">
              （全集群累计 · 合计 {total.toLocaleString()} 请求）
            </span>
          </div>
          {Object.entries(buckets)
            // ⚠ 必须显式按数值排序，**不能依赖对象键序**：
            // JS 会把"整数样"的键（"1"）提到所有普通字符串键（"0.005"、"2.5"）之前，
            // 于是"≤1s"会跑到第一行，分布图看起来非单调（224 → 209）。
            // 后端已按数值排好序，但 JSON→JS 这一步会被这条规则打乱。
            .sort((a, b) => Number(a[0]) - Number(b[0]))
            .map(([le, count]) => {
              const pct = total > 0 ? (Number(count) / total * 100) : 0
              return (
                <div key={le} className="bucket-row">
                  <span className="bucket-le">≤{le}s</span>
                  <div className="bucket-bar-bg">
                    <div className="bucket-bar-fill" style={{ width: `${pct}%` }} />
                  </div>
                  <span className="bucket-count">{Number(count).toLocaleString()}</span>
                </div>
              )
            })}
          <div className="metrics-hint">
            累计直方图：每个桶是"≤该延迟的请求数"，按 le 从 Prometheus 实际返回值生成
            （不再硬编码 le 字符串 —— 曾经因为查 `le="1.0"` 而实际 label 是 `le="1"`，
            导致分布图上出现"≤1.0s 比 ≤2.5s 还少"的矛盾数据）。
          </div>
        </div>
      )}

      {/* Transaction */}
      {data.txn && (
        <div className="metrics-section">
          <div className="metrics-title">💳 交易汇总</div>
          <div className="metric-grid">
            <MetricCard label="SETTLED" value={data.txn.settled} unit="" />
            <MetricCard label="PENDING" value={data.txn.pending} unit="" />
            <MetricCard label="FAILED" value={data.txn.failed} unit="" />
          </div>
        </div>
      )}
    </div>
  )
}

function MetricCard({ label, value, unit, title, warn }: {
  label: string; value: string; unit: string; title?: string; warn?: boolean
}) {
  const num = parseFloat(value)
  const isOk = (!isNaN(num) && num === 0) || value === 'N/A'

  return (
    <div className="metric-card" title={title}>
      <div className="metric-label">{label}</div>
      <div className="metric-value"
           style={{ color: warn ? 'var(--red)' : isOk ? 'var(--text-muted)' : 'var(--accent)' }}>
        {value}{unit && value !== 'N/A' ? ` ${unit}` : ''}
      </div>
    </div>
  )
}
