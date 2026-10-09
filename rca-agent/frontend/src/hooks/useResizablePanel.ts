import { useCallback, useEffect, useRef, useState } from 'react'

interface Options {
  /** localStorage 键名 */
  storageKey: string
  /** 默认宽度(px) */
  defaultWidth: number
  /** 面板最小宽度(px) */
  minWidth?: number
  /** 左侧面板最小宽度(px) */
  minOtherWidth?: number
  /** 面板最大宽度(px) */
  maxWidth?: number
}

interface Result {
  /** 当前面板宽度(px) */
  width: number
  /** 是否正在拖拽 */
  dragging: boolean
  /** 是否已折叠 */
  collapsed: boolean
  /** 容器 ref（挂在 flex 容器上） */
  containerRef: React.RefObject<HTMLDivElement>
  /** 分隔条拖拽起始 */
  onHandleMouseDown: (e: React.MouseEvent) => void
  /** 分隔条触摸起始 */
  onHandleTouchStart: (e: React.TouchEvent) => void
  /** 分隔条键盘操作 */
  onHandleKeyDown: (e: React.KeyboardEvent) => void
  /** 双击复位 */
  onHandleDoubleClick: () => void
  /** 折叠/展开切换 */
  toggleCollapsed: () => void
  /** 手动设置宽度 */
  setWidth: (w: number) => void
}

function readStored(key: string, fallback: number): number {
  try {
    const v = localStorage.getItem(key)
    if (v === null) return fallback
    const n = Number(v)
    return Number.isFinite(n) && n > 0 ? n : fallback
  } catch {
    return fallback
  }
}

/**
 * 右侧面板可拖拽调宽。
 *
 * 以"右侧面板宽度"为状态（而非左侧），因为拖拽时鼠标 X 与容器右边界的
 * 距离就是面板宽度，计算最直观，也天然适配容器尺寸变化。
 */
export function useResizablePanel({
  storageKey,
  defaultWidth,
  minWidth = 280,
  minOtherWidth = 320,
  maxWidth = 1100,
}: Options): Result {
  const containerRef = useRef<HTMLDivElement>(null)
  const [width, setWidthState] = useState<number>(() => readStored(storageKey, defaultWidth))
  const [dragging, setDragging] = useState(false)
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(storageKey + ':collapsed') === '1'
    } catch {
      return false
    }
  })

  /** 依据容器宽度夹取合法宽度 */
  const clamp = useCallback(
    (w: number): number => {
      const total = containerRef.current?.clientWidth ?? 0
      // 容器太窄时退化为均分，避免出现负值
      const hardMax = total > 0 ? Math.max(minWidth, total - minOtherWidth) : maxWidth
      const upper = Math.min(maxWidth, hardMax)
      const lower = Math.min(minWidth, upper)
      return Math.round(Math.min(Math.max(w, lower), upper))
    },
    [minWidth, minOtherWidth, maxWidth],
  )

  const setWidth = useCallback((w: number) => setWidthState(clamp(w)), [clamp])

  // ── 拖拽（鼠标）──────────────────────────────────────────────────
  const onHandleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    setDragging(true)
  }, [])

  // ── 拖拽（触摸）──────────────────────────────────────────────────
  const onHandleTouchStart = useCallback((_e: React.TouchEvent) => {
    setDragging(true)
  }, [])

  useEffect(() => {
    if (!dragging) return

    const applyFromClientX = (clientX: number) => {
      const rect = containerRef.current?.getBoundingClientRect()
      if (!rect) return
      setWidthState(clamp(rect.right - clientX))
    }

    const onMouseMove = (ev: MouseEvent) => {
      ev.preventDefault()
      applyFromClientX(ev.clientX)
    }
    const onMouseUp = () => setDragging(false)

    const onTouchMove = (ev: TouchEvent) => {
      if (ev.touches.length > 0) {
        ev.preventDefault()
        applyFromClientX(ev.touches[0].clientX)
      }
    }
    const onTouchEnd = () => setDragging(false)

    // 拖拽期间禁止选中文本 / 保持 col-resize 光标
    const prevCursor = document.body.style.cursor
    const prevSelect = document.body.style.userSelect
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'

    document.addEventListener('mousemove', onMouseMove)
    document.addEventListener('mouseup', onMouseUp)
    document.addEventListener('touchmove', onTouchMove, { passive: false })
    document.addEventListener('touchend', onTouchEnd)
    document.addEventListener('touchcancel', onTouchEnd)

    return () => {
      document.removeEventListener('mousemove', onMouseMove)
      document.removeEventListener('mouseup', onMouseUp)
      document.removeEventListener('touchmove', onTouchMove)
      document.removeEventListener('touchend', onTouchEnd)
      document.removeEventListener('touchcancel', onTouchEnd)
      document.body.style.cursor = prevCursor
      document.body.style.userSelect = prevSelect
    }
  }, [dragging, clamp])

  // ── 持久化 ──────────────────────────────────────────────────────
  useEffect(() => {
    try {
      localStorage.setItem(storageKey, String(width))
    } catch {
      /* 忽略隐私模式等写入失败 */
    }
  }, [storageKey, width])

  useEffect(() => {
    try {
      localStorage.setItem(storageKey + ':collapsed', collapsed ? '1' : '0')
    } catch {
      /* 忽略 */
    }
  }, [storageKey, collapsed])

  // ── 容器尺寸变化时重新夹取 ──────────────────────────────────────
  useEffect(() => {
    const el = containerRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => {
      setWidthState(w => clamp(w))
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [clamp])

  // ── 键盘操作 ────────────────────────────────────────────────────
  const onHandleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      const STEP = e.shiftKey ? 60 : 20
      if (e.key === 'ArrowLeft') {
        e.preventDefault()
        setWidthState(w => clamp(w + STEP)) // 面板变宽
      } else if (e.key === 'ArrowRight') {
        e.preventDefault()
        setWidthState(w => clamp(w - STEP)) // 面板变窄
      } else if (e.key === 'Home') {
        e.preventDefault()
        setWidthState(clamp(defaultWidth))
      } else if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        setCollapsed(c => !c)
      }
    },
    [clamp, defaultWidth],
  )

  const onHandleDoubleClick = useCallback(() => {
    setWidthState(clamp(defaultWidth))
  }, [clamp, defaultWidth])

  const toggleCollapsed = useCallback(() => setCollapsed(c => !c), [])

  return {
    width,
    dragging,
    collapsed,
    containerRef,
    onHandleMouseDown,
    onHandleTouchStart,
    onHandleKeyDown,
    onHandleDoubleClick,
    toggleCollapsed,
    setWidth,
  }
}
