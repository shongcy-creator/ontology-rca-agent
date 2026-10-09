/**
 * 「正在运行的任务」的跨页面状态。
 *
 * ## 为什么需要它（实测问题）
 *
 * `App.tsx` 用 `{tab === 'stress' && <StressPage />}` 这种写法切页 —— 也就是说
 * **切走就卸载**。压测任务本身跑在后端（subprocess），并不会因为切页而停；
 * 但页面一卸载，本地 state 里的 job id、实时日志、"运行中"指示全没了，
 * 轮询也被 cleanup 清掉。用户切到"智能诊断"再切回来，看到的是一片空闲，
 * **看起来就像压测被中断了**（实测后端其实跑完了 122.9s / 453 次请求 / 0 失败）。
 *
 * 这类"状态只活在组件里"的问题在长任务界面上代价很高：
 * 用户无法离开页面去干别的，也无法确认任务是否还在跑。
 *
 * ## 做法
 *
 * 把"当前任务"这一个事实放进 localStorage 并广播变更：
 *   · 页面挂载时**重新接上**（还在跑就恢复轮询与日志，已结束就显示结果）；
 *   · 顶部导航据此显示全局指示，切到任何页面都能看到"有任务在跑"。
 *
 * 注意：这里**只存"哪个任务"**（id + 类型 + 标签），不存任务内容 ——
 * 任务的真实状态始终以**后端**为准（`/api/chaos/jobs/{id}`），
 * 避免本地缓存与后端分叉。
 */

export interface RunningJob {
  id: string
  kind: 'stress' | 'chaos' | 'other'
  /** 展示用标签（如压测模式 / 故障标题） */
  label: string
  /** 启动时刻（本地毫秒） */
  startedAt: number
}

const KEY = 'cc-ops:running-job'
const listeners = new Set<(j: RunningJob | null) => void>()

function read(): RunningJob | null {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw) return null
    const j = JSON.parse(raw) as RunningJob
    return j && j.id ? j : null
  } catch {
    return null
  }
}

function write(j: RunningJob | null): void {
  try {
    if (j) window.localStorage.setItem(KEY, JSON.stringify(j))
    else window.localStorage.removeItem(KEY)
  } catch {
    /* 隐私模式等：退化为"不持久化"，功能仍可用 */
  }
  listeners.forEach((fn) => fn(j))
}

export const runningJob = {
  get: read,

  /** 记下"开始了一个任务"。 */
  start(job: Omit<RunningJob, 'startedAt'> & { startedAt?: number }): RunningJob {
    const j: RunningJob = { ...job, startedAt: job.startedAt ?? Date.now() }
    write(j)
    return j
  },

  /**
   * 任务结束时清除。
   * 传 id 是为了避免"迟到的回调把新任务的状态清掉"（同一页面可能连续发起两次）。
   */
  finish(id?: string): void {
    const cur = read()
    if (!cur) return
    if (id && cur.id !== id) return
    write(null)
  },

  subscribe(fn: (j: RunningJob | null) => void): () => void {
    listeners.add(fn)
    // 跨标签页也要同步（同一浏览器开了两个控制台时）
    const onStorage = (e: StorageEvent) => { if (e.key === KEY) fn(read()) }
    window.addEventListener('storage', onStorage)
    return () => { listeners.delete(fn); window.removeEventListener('storage', onStorage) }
  },
}
