/**
 * payment-app: Node.js Express-style server with Prometheus metrics.
 *
 * Cluster-aware variant (v1.8.3):
 *   · 每副本独立身份（APP_INSTANCE）→ 所有指标带 app_instance 标签
 *   · 读写分离：写走主库（DB_HOST），读走只读副本（DB_REPLICA_HOSTS 轮询）
 *   · 连接池可观测：active / idle / waiting / limit，用于连接池耗尽类 RCA
 *   · 容器级资源可观测：cgroup 内存用量/上限、CPU 节流计数（OOM/节流类 RCA）
 *
 * Endpoints:
 *   GET  /health          — liveness probe（含实例身份、DB 主库/副本可达性）
 *   GET  /metrics         — Prometheus text format (prom-client)
 *   GET  /instance        — 本副本身份与上游/下游拓扑
 *   GET  /topology        — JSON topology summary（读副本）
 *   GET  /topology/raw    — JSON full snapshot + MySQL probe（主库 + 副本）
 *   GET  /txn/recent      — 从只读副本读取最近交易（验证读写分离与复制延迟）
 *   POST /txn             — simulated transaction（写主库）
 *
 * Metrics exposed (Prometheus text format):
 *   # HELP cc_http_requests_total   Total HTTP requests by method, path, status
 *   # HELP cc_http_request_duration_seconds  HTTP request latency histogram
 *   # HELP cc_mysql_pool_active     Active MySQL connections
 *   # HELP cc_mysql_pool_idle       Idle MySQL connections
 *   # HELP cc_mysql_pool_waiting    Queued connection requests (pool starvation)
 *   # HELP cc_mysql_pool_limit      MySQL connection pool limit
 *   # HELP cc_mysql_query_duration_seconds  MySQL query latency histogram
 *   # HELP cc_mysql_up              1 = primary reachable, 0 = down
 *   # HELP cc_mysql_replica_up      Per-replica reachability (label replica)
 *   # HELP cc_mysql_replica_lag_reads  Rows the write path leads the read path by
 *   # HELP cc_txn_total             Transaction counter by status (settled/pending/failed)
 *   # HELP cc_txn_amount_total      Transaction amount sum by status
 *   # HELP cc_container_cpu_seconds Total process CPU seconds
 *   # HELP cc_container_memory_bytes Process resident memory bytes
 *   # HELP cc_container_memory_working_set_bytes Container cgroup memory usage (bytes)
 *   # HELP cc_container_memory_limit_bytes  Container cgroup memory limit (bytes)
 *   # HELP cc_container_cpu_nr_throttled_total Number of throttled CPU periods
 *   # HELP cc_container_cpu_throttled_seconds_total Total CPU throttled time (s)
 */

const http = require('http');
const os   = require('os');
const fs   = require('fs');
const path = require('path');

// ---------------------------------------------------------------- env
const APP_NAME    = process.env.APP_NAME    || 'payment-app';
const APP_VERSION = process.env.APP_VERSION || '1.8.3';
const PORT        = Number(process.env.PORT || 8080);

// 实例身份：compose 下由 HOSTNAME（容器 ID）或显式 APP_INSTANCE 决定
const INSTANCE_ID = process.env.APP_INSTANCE
  || process.env.HOSTNAME
  || os.hostname();

const DB_HOST     = process.env.DB_HOST     || 'mysql';
const DB_PORT     = Number(process.env.DB_PORT || 3306);
const DB_USER     = process.env.DB_USER     || 'appuser';
const DB_PASSWORD = process.env.DB_PASSWORD || 'apppass';
const DB_NAME     = process.env.DB_NAME     || 'creditcard';

// 只读副本列表（逗号分隔的 host:port 或 host）；为空时读也走主库
const DB_REPLICA_HOSTS = (process.env.DB_REPLICA_HOSTS || '')
  .split(',').map(s => s.trim()).filter(Boolean);

const POOL_LIMIT = Number(process.env.POOL_LIMIT || 10);

const STARTED_AT = new Date().toISOString();

// ---------------------------------------------------------------- cgroup 采集
// 集群化后"资源不足"是最常见根因类别，而 Node 自身的 process.memoryUsage()
// 看不到 **容器** 级别的用量：`docker exec` 起的 stress/mem-hog 进程与 PID 1
// 同属一个 cgroup，必须读 cgroup 才能观测到。这里在每次 scrape 时读取，
// 暴露容器真实的 memory 用量/上限与 CPU 节流计数（cgroup v2 优先，回退 v1）。
function readCgroup() {
  const out = {
    memCurrent: null, memMax: null,
    nrThrottled: null, throttledUsec: null, throttledPeriods: null, nrPeriods: null,
    oomKill: null, oom: null,
    version: null,
  };
  const tryNum = (p) => {
    try { return Number(fs.readFileSync(p, 'utf8').trim()); } catch (_) { return null; }
  };
  // cgroup v2 的 memory.events 是**只增的计数器**（oom_kill / oom / max），
  // 正好适合当"是否发生过 OOM Kill"的证据（fetch_/restart 计数在容器内拿不到）。
  const readMemEventsV2 = (o) => {
    let txt = '';
    try { txt = fs.readFileSync('/sys/fs/cgroup/memory.events', 'utf8'); } catch (_) { return; }
    for (const line of txt.split('\n')) {
      const [k, val] = line.trim().split(/\s+/);
      if (k === 'oom_kill') o.oomKill = Number(val);
      if (k === 'oom') o.oom = Number(val);
    }
  };
  // ── cgroup v2 ─────────────────────────────────────────────
  let v = tryNum('/sys/fs/cgroup/memory.current');
  if (v !== null) {
    out.version = 'v2';
    out.memCurrent = v;
    const max = fs.existsSync('/sys/fs/cgroup/memory.max')
      ? fs.readFileSync('/sys/fs/cgroup/memory.max', 'utf8').trim() : '';
    out.memMax = (max === 'max' || max === '') ? null : Number(max);
    readMemEventsV2(out);
    const cpuStat = (() => {
      try { return fs.readFileSync('/sys/fs/cgroup/cpu.stat', 'utf8'); } catch (_) { return ''; }
    })();
    for (const line of cpuStat.split('\n')) {
      const [k, val] = line.trim().split(/\s+/);
      if (k === 'nr_throttled') out.nrThrottled = Number(val);
      if (k === 'throttled_usec') out.throttledUsec = Number(val);
      if (k === 'nr_periods') out.nrPeriods = Number(val);
      if (k === 'throttled_periods') out.throttledPeriods = Number(val);
    }
    return out;
  }
  // ── cgroup v1 回退 ────────────────────────────────────────
  v = tryNum('/sys/fs/cgroup/memory/memory.usage_in_bytes');
  if (v !== null) {
    out.version = 'v1';
    out.memCurrent = v;
    const lim = tryNum('/sys/fs/cgroup/memory/memory.limit_in_bytes');
    out.memMax = (lim && lim < 1e15) ? lim : null;
    // v1：oom_control 里只有"当前是否被 OOM 禁用"，没有计数；
    // 退而用 memory.failcnt（达到上限被拒的次数），语义上最接近。
    const failcnt = tryNum('/sys/fs/cgroup/memory/memory.failcnt');
    if (failcnt !== null) out.oomKill = failcnt;
    const cpuStat = (() => {
      try { return fs.readFileSync('/sys/fs/cgroup/cpu/cpu.stat', 'utf8'); } catch (_) { return ''; }
    })();
    for (const line of cpuStat.split('\n')) {
      const [k, val] = line.trim().split(/\s+/);
      if (k === 'nr_throttled') out.nrThrottled = Number(val);
      if (k === 'throttled_time') out.throttledUsec = Math.round(Number(val) / 1000);
      if (k === 'nr_periods') out.nrPeriods = Number(val);
    }
  }
  return out;
}

// ---------------------------------------------------------------- Prometheus metrics (lazy — avoid crash if prom-client not installed)
let register, Counter, Histogram, Gauge;
let metrics = {};

function initMetrics() {
  try {
    ({ register, Counter, Histogram, Gauge } = require('prom-client'));
    const g  = new Gauge({
      name: 'cc_mysql_pool_active', help: 'Active MySQL connections',
      collect() { const s = _poolSnapshot(); if (s.ok) this.set(s.active); },
    });
    const h  = new Gauge({
      name: 'cc_mysql_pool_idle', help: 'Idle MySQL connections',
      collect() { const s = _poolSnapshot(); if (s.ok) this.set(s.idle); },
    });
    const w  = new Gauge({
      name: 'cc_mysql_pool_waiting', help: 'Queued connection requests (pool starvation)',
      collect() { const s = _poolSnapshot(); if (s.ok) this.set(s.waiting); },
    });
    // 连接池内部结构是否可读（1/0）。恒为 0 的 active/idle 会伪装成"查过了没问题"，
    // 所以必须有一个显式的"指标是否可信"信号暴露出来。
    const poolok = new Gauge({
      name: 'cc_mysql_pool_metrics_ok',
      help: 'Whether connection-pool internals could be read (1=trustworthy, 0=metrics degraded)',
      collect() { const s = _poolSnapshot(); this.set(s.ok ? 1 : 0); },
    });
    // ⚠ cc_mysql_pool_limit 是**配置值**，不是"运行时状态"，必须在每次抓取时都正确。
    //
    // 踩过的坑：它原先只在 `getDb()`（写路径首次建池）和 `updatePoolMetrics()` 里 set，
    // 于是**只服务过读请求的副本**从没触发过 → gauge 保持默认 0 →
    // Prometheus 上出现"3 个副本里 1 个 limit=0"的假数据，面板显示"连接上限 0"。
    // 用 prom-client 的 `collect()` 回调：每次 /metrics 被抓取时都会重新求值，
    // 与"该副本有没有处理过写请求"彻底解耦。
    const k  = new Gauge({
      name: 'cc_mysql_pool_limit',
      help: 'MySQL connection pool limit',
      collect() { this.set(POOL_LIMIT); },
    });
    k.set(POOL_LIMIT);   // 首次抓取之前也先有正确值
    const up = new Gauge({ name: 'cc_mysql_up',           help: 'Primary MySQL reachable (1/0)' });
    const rup = new Gauge({
      name: 'cc_mysql_replica_up', help: 'Read replica reachable (1/0)',
      labelNames: ['replica'],
    });
    const rlag = new Gauge({
      name: 'cc_mysql_replica_lag_reads',
      help: 'How many writes the primary leads the observed replica read by',
    });
    // 副本的**复制延迟配置态**（SOURCE_DELAY / DESIRED_DELAY）。
    // 为什么需要：`Seconds_Behind_Master` 是延迟的**后果**，故障解除后仍要几分钟
    // 排空积压；拿它做恢复校验会稳定误判为"回滚失败"。配置态则瞬时归零。
    const rdelay = new Gauge({
      name: 'cc_mysql_replica_desired_delay',
      help: 'Replica configured replication delay in seconds (SOURCE_DELAY target)',
      labelNames: ['replica'],
    });
    const txc  = new Counter({ name: 'cc_txn_total',      help: 'Transaction counter', labelNames: ['status'] });
    const txa  = new Gauge({  name: 'cc_txn_amount_total', help: 'Transaction amount sum', labelNames: ['status'] });
    const dbq  = new Histogram({
      name: 'cc_mysql_query_duration_seconds',
      help: 'MySQL query latency',
      buckets: [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5]
    });
    const httph = new Histogram({
      name: 'cc_http_request_duration_seconds',
      help: 'HTTP request latency',
      labelNames: ['method', 'path', 'status'],
      buckets: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5]
    });
    const httpc = new Counter({
      name: 'cc_http_requests_total',
      help: 'Total HTTP requests',
      labelNames: ['method', 'path', 'status']
    });
    const cpug = new Gauge({ name: 'cc_container_cpu_seconds',   help: 'Process CPU seconds total' });
    const memg = new Gauge({ name: 'cc_container_memory_bytes', help: 'Process resident memory bytes' });

    // ── 容器级（cgroup）资源指标：OOM/CPU 节流类根因的直接证据 ──
    const cMem = new Gauge({
      name: 'cc_container_memory_working_set_bytes',
      help: 'Container cgroup memory usage (bytes)',
      collect() {
        const c = readCgroup();
        if (c.memCurrent !== null) this.set(c.memCurrent);
        if (c.memMax !== null) cMemLimit.set(c.memMax);
        if (c.nrThrottled !== null) cThrottled.set(c.nrThrottled);
        if (c.throttledUsec !== null) cThrottledUsec.set(c.throttledUsec / 1e6);
        if (c.oomKill !== null) cOomKill.set(c.oomKill);
      },
    });
    const cMemLimit = new Gauge({
      name: 'cc_container_memory_limit_bytes',
      help: 'Container cgroup memory limit (bytes); 0 = unlimited',
    });
    const cThrottled = new Gauge({
      name: 'cc_container_cpu_nr_throttled_total',
      help: 'Number of cgroup CPU periods in which tasks were throttled',
    });
    const cThrottledUsec = new Gauge({
      name: 'cc_container_cpu_throttled_seconds_total',
      help: 'Total time (s) tasks were throttled by the CPU quota',
    });
    // 容器 OOM Kill 计数（cgroup memory.events 的 oom_kill，**只增**）。
    // 为什么必须有它：应用层原先只有"内存逼近上限"（working_set/limit）这一种证据，
    // 而 OOM Kill 发生后容器会重启、内存回落 —— 压力类告警随之消失，
    // 于是"被 OOM 杀过"这件事在指标上**完全不可观测**，也就无法被告警覆盖。
    const cOomKill = new Gauge({
      name: 'cc_container_oom_kill_total',
      help: 'Container cgroup OOM kill count (monotonic)',
    });
    const info = new Gauge({
      name: 'cc_app_instance_info', help: 'App instance metadata (always 1)',
      labelNames: ['app_instance', 'version'],
    });

    metrics = { g, h, w, k, poolok, up, rup, rlag, rdelay, txc, txa, dbq, httph, httpc, cpug, memg,
                cMem, cMemLimit, cThrottled, cThrottledUsec, cOomKill, info, register };
    // 注意：身份标签命名为 app_instance（而非 instance），避免与 Prometheus
    // 目标标签 instance 冲突被改写成 exported_instance。
    register.setDefaultLabels({ app: APP_NAME, app_instance: INSTANCE_ID, version: APP_VERSION });
    info.set({ app_instance: INSTANCE_ID, version: APP_VERSION }, 1);
    // collect default Node.js metrics
    require('prom-client').collectDefaultMetrics({ register });
    console.log('[metrics] prom-client initialized, instance=' + INSTANCE_ID);
  } catch (e) {
    console.log('[metrics] prom-client unavailable:', e.message);
  }
}

initMetrics();

// ---------------------------------------------------------------- MySQL driver (lazy, cached pools)
let mysqlDriver = null;
let dbPool = null;              // 主库池（读写）
const replicaPools = new Map(); // 副本池（只读）
let replicaRR = 0;              // 副本轮询游标

function _loadDriver() {
  if (mysqlDriver) return mysqlDriver;
  try { mysqlDriver = require('mysql2/promise'); }
  catch (e) { return null; }
  return mysqlDriver;
}

function _poolOptions(host, port) {
  return {
    host, port: port || DB_PORT, user: DB_USER, password: DB_PASSWORD,
    database: DB_NAME, waitForConnections: true, connectionLimit: POOL_LIMIT,
    queueLimit: 0,
  };
}

function getDb() {
  if (dbPool) return dbPool;
  const drv = _loadDriver();
  if (!drv) return null;
  dbPool = drv.createPool(_poolOptions(DB_HOST, DB_PORT));
  if (metrics.k) metrics.k.set(POOL_LIMIT);
  return dbPool;
}

function parseReplica(spec) {
  const [host, port] = String(spec).split(':');
  return { host: host.trim(), port: Number(port || DB_PORT), spec: String(spec).trim() };
}

function getReplica() {
  if (!DB_REPLICA_HOSTS.length) return null;
  const drv = _loadDriver();
  if (!drv) return null;
  const spec = DB_REPLICA_HOSTS[replicaRR % DB_REPLICA_HOSTS.length];
  replicaRR += 1;
  const { host, port, spec: key } = parseReplica(spec);
  if (!replicaPools.has(key)) {
    // 副本池刻意给小：副本主要服务读探针，不承担写入
    replicaPools.set(key, drv.createPool(_poolOptions(host, port)));
  }
  return { key, host, port, pool: replicaPools.get(key) };
}

// 读一个"类数组/集合"的长度。mysql2 v3 内部用的是自定义 RingQueue（不是 Array），
// 只判断 Array.isArray 会全部读不到 → 三个 gauge 永远是 0。
function _len(v) {
  if (v === null || v === undefined) return null;
  if (typeof v === 'number') return v;
  if (Array.isArray(v)) return v.length;
  if (typeof v.length === 'number') return v.length;   // RingQueue / 类数组
  if (typeof v.size === 'number') return v.size;       // Set / Map
  return null;
}

/**
 * 从一个 mysql2 连接池读出 {active, idle, waiting}；读不到返回 null。
 *
 * ⚠ 这些数字是 RCA 的证据来源（判定式 `active / limit > 80%`）。
 * 恒为 0 会让那条证据**永远算出 0%、永远不触发** —— 一个"恒为绿"的观测
 * 比没有观测更危险，因为它看起来像"查过了，确实没问题"。
 *
 * 踩过的坑：mysql2 升到 3.24.5 后 `_allConnections/_freeConnections/_connectionQueue`
 * 变成自定义 `RingQueue`（有 `.length` 但不是 Array），而原实现只认 `Array.isArray`
 * → active/idle/waiting **全部恒为 0**，且异常被 catch 静默吞掉，长期无人发现。
 */
function _poolCounts(pool) {
  try {
    const inner = (pool && (pool.pool || pool)) || null;
    if (!inner) return null;
    const allLen = _len(inner._allConnections);
    if (allLen === null) return null;
    const freeLen = _len(inner._freeConnections);
    const idle = freeLen === null ? 0 : freeLen;
    return {
      active: Math.max(0, allLen - idle),   // _allConnections 含空闲与占用
      idle,
      waiting: _len(inner._connectionQueue) || 0,
    };
  } catch (_) {
    return null;
  }
}

/** 聚合写池 + 所有副本池（抓取时求值，不依赖"有没有走过某条代码路径"）。 */
function _poolSnapshot() {
  let active = 0, idle = 0, waiting = 0, ok = 0, pools = 0;
  const all = [];
  if (dbPool) all.push(dbPool);
  for (const p of replicaPools.values()) all.push(p);
  for (const p of all) {
    const c = _poolCounts(p);
    if (!c) continue;
    pools += 1; ok = 1;
    active += c.active; idle += c.idle; waiting += c.waiting;
  }
  return { active, idle, waiting, ok, pools };
}

// 兼容旧调用点：写路径上顺手刷新一次（真正的正确性由 scrape 时的 collect() 保证）
function updatePoolMetrics() {
  if (!metrics.g) return;
  const s = _poolSnapshot();
  if (!s.ok) { metrics.poolReadable = false; return; }
  if (metrics.g) metrics.g.set(s.active);
  if (metrics.h) metrics.h.set(s.idle);
  if (metrics.w) metrics.w.set(s.waiting);
  metrics.poolReadable = true;
}

// ---------------------------------------------------------------- http server
const server = http.createServer(async (req, res) => {
  const t0 = Date.now();

  // CORS preflight
  res.setHeader('Access-Control-Allow-Origin', '*');
  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }

  // Update system gauges every request
  if (metrics.cpug) {
    const rss = process.memoryUsage();
    metrics.cpug.set(process.cpuUsage().user / 1e6);
    metrics.memg.set(rss.rss);
  }

  try {
    if (req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        ok: true, app: APP_NAME, version: APP_VERSION, instance: INSTANCE_ID,
      }));
    }
    else if (req.url === '/instance') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        instance: INSTANCE_ID,
        app: APP_NAME,
        version: APP_VERSION,
        hostname: os.hostname(),
        pid: process.pid,
        containerIP: getPrimaryIP(),
        poolLimit: POOL_LIMIT,
        primary: { host: DB_HOST, port: DB_PORT, database: DB_NAME },
        replicas: DB_REPLICA_HOSTS,
        startedAt: STARTED_AT,
      }, null, 2));
    }
    else if (req.url === '/metrics') {
      const reg = metrics.register || register;
      if (!reg) { res.writeHead(503); res.end('metrics unavailable'); return; }
      // 采样时刷新进程级资源指标，保证无流量时也是新鲜的
      if (metrics.cpug) metrics.cpug.set(process.cpuUsage().user / 1e6);
      if (metrics.memg) metrics.memg.set(process.memoryUsage().rss);
      res.setHeader('Content-Type', reg.contentType);
      res.end(await reg.metrics());
    }
    else if (req.url === '/txn/recent' || req.url === '/txn/recent/') {
      const out = await readRecentTxns(10);
      res.writeHead(out.ok ? 200 : 503, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(out, null, 2));
    }
    else if (req.url === '/topology' || req.url === '/topology/') {
      const snap = await rawTopologySnapshot({ brief: true });
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify(snap, null, 2));
    }
    else if (req.url === '/topology/raw' || req.url === '/topology/raw/') {
      const snap = await rawTopologySnapshot({ brief: false });
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify(snap, null, 2));
    }
    else if (req.method === 'POST' && req.url === '/txn') {
      // 同步读取 body（数据量小）
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const body = chunks.join('');
      try {
        const { customer_id = 1, amount = 0, status = 'SETTLED' } = JSON.parse(body || '{}');
        await recordTxn({ customer_id, amount, status });
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ recorded: true, customer_id, amount, status, instance: INSTANCE_ID }));
      } catch (e) {
        res.writeHead(503, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ recorded: false, error: e.message, instance: INSTANCE_ID }));
      }
    }
    else {
      res.writeHead(404); res.end('not found');
    }
    const status = res.statusCode || 200;
    const p = (req.url || '/').split('?')[0].slice(0, 64);
    if (metrics.httpc) metrics.httpc.inc({ method: req.method, path: p, status });
    if (metrics.httph) metrics.httph.observe({ method: req.method, path: p, status }, (Date.now() - t0) / 1000);
  } catch (e) {
    res.writeHead(500); res.end(JSON.stringify({ error: e.message }));
    if (metrics.httpc) metrics.httpc.inc({ method: req.method, path: (req.url||'/').split('?')[0], status: 500 });
  }
});

server.listen(PORT, () => {
  console.log(`[${APP_NAME}] instance=${INSTANCE_ID} listening :${PORT}`);
});

// 定时刷新副本指标（读路径可达性 + 复制延迟配置态）。
// 不这样做的话，这些序列只在有人调 /topology 时才出现 —— 见 probeReplicas 的注释。
// 20s 与 Prometheus 的抓取间隔同量级，且只多开 2 个连接，开销可忽略。
setInterval(() => {
  probeReplicas().catch(() => { /* 探针失败由 gauge=0 体现，不打断进程 */ });
}, 20000);

// ---------------------------------------------------------------- transaction recorder (writes → primary)
async function recordTxn({ customer_id, amount, status }) {
  const pool = getDb();
  if (!pool) throw new Error('mysql2 driver not installed');
  const t0 = Date.now();
  let conn = null;
  try {
    conn = await pool.getConnection();
    updatePoolMetrics(pool);
    await conn.query(
      'INSERT INTO t_txn (customer_id, amount, status) VALUES (?, ?, ?)',
      [customer_id, amount, status]
    );
    if (metrics.dbq) metrics.dbq.observe((Date.now() - t0) / 1000);
    if (metrics.txc) metrics.txc.inc({ status });
    if (metrics.txa) metrics.txa.inc({ status }, amount);
    if (metrics.up) metrics.up.set(1);
  } catch (e) {
    if (metrics.up) metrics.up.set(0);
    if (metrics.txc) metrics.txc.inc({ status: 'failed' });
    throw e;
  } finally {
    // 必须无条件归还连接：早期版本在异常路径上不 release，
    // 导致"写失败 → 连接泄漏 → 连接池耗尽 → 后续请求全部挂起"，
    // 真实故障的 503 会被超时掩盖，RCA 证据链被污染。
    if (conn) { try { conn.release(); } catch (_) { /* ignore */ } }
    updatePoolMetrics(pool);
  }
}

// ---------------------------------------------------------------- read path (reads → replica when configured)
async function readRecentTxns(limit) {
  const target = getReplica();
  const pool = target ? target.pool : getDb();
  if (!pool) return { ok: false, error: 'mysql2 driver not installed' };
  const where = target ? target.key : DB_HOST + ':' + DB_PORT;
  const t0 = Date.now();
  let conn = null;
  try {
    conn = await pool.getConnection();
    const [rows] = await conn.query(
      'SELECT txn_id, customer_id, amount, status, created_at FROM t_txn ORDER BY txn_id DESC LIMIT ?',
      [limit]
    );
    if (metrics.dbq) metrics.dbq.observe((Date.now() - t0) / 1000);
    if (target && metrics.rup) metrics.rup.set({ replica: where }, 1);
    return { ok: true, source: where, latencyMs: Date.now() - t0, rows, instance: INSTANCE_ID };
  } catch (e) {
    if (target && metrics.rup) metrics.rup.set({ replica: where }, 0);
    return { ok: false, source: where, error: e.message, instance: INSTANCE_ID };
  } finally {
    if (conn) { try { conn.release(); } catch (_) { /* ignore */ } }
  }
}

// ---------------------------------------------------------------- topology snapshot
async function rawTopologySnapshot({ brief = false } = {}) {
  const snap = {
    capturedAt: new Date().toISOString(),
    app: {
      name: APP_NAME, version: APP_VERSION,
      instance: INSTANCE_ID, containerName: os.hostname(), pid: process.pid,
    },
    network: { containerIP: getPrimaryIP() },
    db: { host: DB_HOST, port: DB_PORT, user: DB_USER, database: DB_NAME },
    readReplicas: DB_REPLICA_HOSTS,
    pool: { limit: POOL_LIMIT },
  };

  // ── 主库探针（写路径）────────────────────────────────────────
  let probe = { reachable: false, latencyMs: null, error: null };
  try {
    const pool = getDb();
    if (!pool) { probe.error = 'mysql2 driver not installed'; }
    else {
      const t0 = Date.now();
      const conn = await pool.getConnection();
      await conn.ping();
      probe.latencyMs = Date.now() - t0;
      const [rows] = await conn.query(
        "SELECT @@hostname AS dbHost, @@version AS version, @@max_connections AS maxConn, @@wait_timeout AS waitTimeout, @@version_comment AS comment, @@server_id AS serverId, @@read_only AS readOnly"
      );
      probe = { reachable: true, latencyMs: probe.latencyMs, ping: rows[0] };
      updatePoolMetrics(pool);
      if (metrics.up) metrics.up.set(1);
      await conn.release();
    }
  } catch (e) {
    probe.error = e.message;
    if (metrics.up) metrics.up.set(0);
  }
  snap.db.probe = probe;

  // ── 副本探针（读路径）────────────────────────────────────────
  // 抽成独立函数，并**同时**由定时器驱动（见文件末尾的 setInterval）。
  // 为什么必须定时刷新：这些指标原先只在 `/topology` 被调用时才更新，
  // 于是 `cc_mysql_replica_up` / `cc_mysql_replica_desired_delay` 在 Prometheus 里
  // **长期没有序列** —— 没人调拓扑，指标就不存在。
  // 指标应当反映现实，而不是反映"最近有没有人来访问我的某个端点"。
  snap.db.replicas = await probeReplicas();

  if (!brief) {
    snap.host = { hostname: process.env.HOSTNAME || os.hostname() };
    try {
      fs.writeFileSync(path.join(__dirname, 'topology-snapshot.json'), JSON.stringify(snap, null, 2));
    } catch (_) { /* 只读文件系统：忽略 */ }
  }
  return snap;
}

/**
 * 探测所有只读副本：可达性 / 延迟 / **复制延迟配置态（DESIRED_DELAY）**。
 *
 * 单独成顶层函数的原因：既要在 `/topology` 快照里用（读路径是否健康），
 * 也要由**定时器**周期调用 —— 原先这段逻辑只长在 `/topology` 里，
 * 于是 `cc_mysql_replica_up` 等序列在 Prometheus 里长期不存在
 * （没人调拓扑就没有样本）。指标应当反映现实，而不是反映"最近有没有人访问某个端点"。
 */
async function probeReplicas() {
  const out = [];
  if (!DB_REPLICA_HOSTS.length) return out;
  for (const spec of DB_REPLICA_HOSTS) {
    const { host, port, spec: key } = parseReplica(spec);
    const entry = { endpoint: key, host, port, reachable: false, error: null };
    try {
      const drv = _loadDriver();
      if (!drv) throw new Error('mysql2 driver not installed');
      if (!replicaPools.has(key)) replicaPools.set(key, drv.createPool(_poolOptions(host, port)));
      const conn = await replicaPools.get(key).getConnection();
      const t0 = Date.now();
      await conn.ping();
      entry.latencyMs = Date.now() - t0;
      const txnFrag = txnRowsFragment();
      const [rows] = await conn.query(
        "SELECT @@hostname AS dbHost, @@server_id AS serverId, @@read_only AS readOnly, " +
        txnFrag.sql + ", " +
        // 复制延迟的**配置态**（SOURCE_DELAY 的目标值）：
        // 这是"故障本身"，而 Seconds_Behind_Master 是它的**后果** ——
        // 后果在故障解除后还要花几分钟排空积压，因此恢复校验必须看配置态。
        "(SELECT DESIRED_DELAY FROM performance_schema.replication_applier_configuration " +
        " LIMIT 1) AS desiredDelay"
      );
      // 缓存新鲜时用缓存值填回，保证字段类型与语义不变（永远是数字，最多滞后 TTL）
      if (txnFrag.fresh && rows[0]) {
        rows[0].txnRows = _txnRowsCache.value;
      } else if (rows[0] && rows[0].txnRows !== null && rows[0].txnRows !== undefined) {
        _txnRowsCache.value = Number(rows[0].txnRows);
        _txnRowsCache.at = Date.now();
      }
      entry.reachable = true;
      entry.ping = rows[0];
      await conn.release();
      if (metrics.rup) metrics.rup.set({ replica: key }, 1);
      if (metrics.rdelay && rows[0] && rows[0].desiredDelay !== null
          && rows[0].desiredDelay !== undefined) {
        metrics.rdelay.set({ replica: key }, Number(rows[0].desiredDelay));
      }
    } catch (e) {
      entry.error = e.message;
      if (metrics.rup) metrics.rup.set({ replica: key }, 0);
    }
    out.push(entry);
  }
  return out;
}

/**
 * `txnRows` 的 60s 缓存。
 *
 * 为什么：`SELECT COUNT(*) FROM t_txn` 在 483 万行的表上是一次**全索引扫描**，
 * 而它只用于在拓扑/指标里展示"表里有多少行"。副本探针每 20s 跑一次、
 * 每次都要对每个副本查一遍、每个应用副本各跑一次 —— 实测这是应用侧最贵的重复查询。
 * 加 60s TTL 后：一个应用实例一分钟最多查一次，且探针 SQL 里不再带子查询。
 *
 * 语义不变：缓存新鲜时用 `NULL AS txnRows` 占位，取回后**用缓存值覆盖**，
 * 因此消费方（拓扑/指标）看到的仍是一个数字，只是最多滞后 60s。
 * 需要更实时可设 `TXN_ROWS_TTL_MS=0` 关闭缓存。
 */
const TXN_ROWS_TTL_MS = Number(process.env.TXN_ROWS_TTL_MS || 60000);
const _txnRowsCache = { value: null, at: 0 };

function txnRowsFragment() {
  const fresh = _txnRowsCache.value !== null
    && (Date.now() - _txnRowsCache.at) < TXN_ROWS_TTL_MS;
  return {
    fresh,
    sql: fresh ? 'NULL AS txnRows' : '(SELECT COUNT(*) FROM t_txn) AS txnRows',
  };
}

function getPrimaryIP() {
  for (const iface of Object.values(os.networkInterfaces())) {
    for (const i of iface || []) {
      if (i.family === 'IPv4' && !i.internal) return i.address;
    }
  }
  return 'unknown';
}
