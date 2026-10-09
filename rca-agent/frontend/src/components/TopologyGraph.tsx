import { useEffect, useRef, useMemo } from 'react'
import * as d3 from 'd3'
import type { TopologyNode, TopologyEdge, PathHop } from '../stores/chatStore'
import './TopologyGraph.css'

interface Props {
  nodes: TopologyNode[]
  edges?: TopologyEdge[]
  path?: PathHop[]
}

// ── 样式映射 ───────────────────────────────────────────────────────────

const NODE_COLORS: Record<string, string> = {
  entity: '#58a6ff',
  metric: '#3fb950',
  relation: '#bc8cff',
  constraint: '#d29922',
  evidence: '#3fb950',
  default: '#8b949e',
}

const NODE_LABELS: Record<string, string> = {
  entity: '实体',
  metric: '指标',
  relation: '关系',
  constraint: '约束',
  evidence: '证据',
}

// scope → 图层（用于分层布局）
const SCOPE_LAYERS: Record<string, number> = {
  '应用层': 0,
  '运行环境层': 1,
  '数据库层': 2,
  'RCA 支持': 3,
}

const EDGE_COLORS: Record<string, string> = {
  composition: '#d29922',
  association: '#58a6ff',
  derivation: '#bc8cff',
  hierarchy: '#3fb950',
  equivalence: '#8b949e',
}

/** 短标签：取 id 冒号后的部分 */
function shortLabel(id: string): string {
  const parts = id.split(':')
  const tail = parts[parts.length - 1]
  return tail.length > 12 ? tail.slice(0, 11) + '…' : tail
}

export function TopologyGraph({ nodes, edges = [], path = [] }: Props) {
  const svgRef = useRef<SVGSVGElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  // 路径上的节点/边集合（用于高亮）
  const { pathNodes, pathEdges } = useMemo(() => {
    const pn = new Set<string>()
    const pe = new Set<string>()
    for (const hop of path) {
      pn.add(hop.from)
      pn.add(hop.to)
      if (hop.relation) pe.add(hop.relation)
    }
    return { pathNodes: pn, pathEdges: pe }
  }, [path])

  useEffect(() => {
    if (!svgRef.current || !containerRef.current || nodes.length === 0) return

    const width = containerRef.current.clientWidth || 340
    const height = Math.max(340, Math.min(700, nodes.length * 42))

    const svg = d3.select(svgRef.current)
    svg.selectAll('*').remove()
    svg.attr('viewBox', `0 0 ${width} ${height}`)
      .attr('width', '100%')
      .attr('height', height)

    // ── 数据准备 ──────────────────────────────────────────────────
    const nodeIds = new Set(nodes.map(n => n.id))
    const validEdges = edges.filter(e => nodeIds.has(e.source) && nodeIds.has(e.target))

    const simNodes = nodes.map(n => ({ ...n }))
    const simLinks = validEdges.map(e => ({
      ...e,
      source: e.source,
      target: e.target,
    }))

    // ── 缩放容器 ──────────────────────────────────────────────────
    // scaleExtent 下限必须足够小：54 个节点挤在 ~230px 宽的详情栏里时，
    // "适应窗口"需要的缩放比可能只有 0.2~0.3；下限设 0.4 会把 fitView 卡住，
    // 表现就是"自适应了但节点还是溢出被裁掉"。
    const root = svg.append('g')
    const zoom = d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.1, 3])
      .on('zoom', ev => root.attr('transform', ev.transform.toString()))
    svg.call(zoom)

    // ── 力导向布局 ────────────────────────────────────────────────
    const radius = (d: any) => (d.type === 'metric' ? 12 : 18)

    // 右侧详情栏很窄（可能只有 200~300px）。用固定的斥力/边长会把节点甩到
    // 视图外面 —— 表现为"拓扑有了但一半节点看不见"。窄栏时按比例收敛。
    const narrow = width < 460
    const linkDist = narrow ? 34 : 70
    const charge = narrow ? -70 : -320

    const simulation = d3.forceSimulation(simNodes as any)
      // 默认 alphaDecay 要跑 ~5s 才收敛，用户点开「拓扑」标签页时看到的正是
      // "还在飞的中间态"，而且此时还没触发 fitView → 看起来仍然是乱的/被裁的。
      // 调快衰减 + 下面的兜底定时器，让 1~2s 内就稳定并自适应。
      .alphaDecay(0.045)
      .force('link', d3.forceLink(simLinks as any)
        .id((d: any) => d.id)
        .distance(linkDist)
        .strength(0.6))
      .force('charge', d3.forceManyBody().strength(charge))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collide', d3.forceCollide().radius((d: any) => radius(d) + (narrow ? 6 : 14)))
      .force('y', d3.forceY((d: any) => {
        const layer = SCOPE_LAYERS[d.scope] ?? 1.5
        return 60 + layer * ((height - 120) / 3)
      }).strength(0.22))

    /**
     * 把整张图"适应到视图内"。
     *
     * 力导向布局的节点坐标是**不受 viewBox 约束**的，节点会跑到 SVG 之外。
     * 这里在布局稳定后计算所有节点的包围盒，反解一个 zoom 变换把它缩放到容器里，
     * 保证"一屏看得到全图"，而不是被裁掉一半。
     */
    const fitView = (animate = true) => {
      const xs = simNodes.map((d: any) => d.x).filter((v: any) => typeof v === 'number')
      const ys = simNodes.map((d: any) => d.y).filter((v: any) => typeof v === 'number')
      if (xs.length === 0 || ys.length === 0) return
      const minX = Math.min(...xs), maxX = Math.max(...xs)
      const minY = Math.min(...ys), maxY = Math.max(...ys)
      const gw = Math.max(1, maxX - minX)
      const gh = Math.max(1, maxY - minY)
      const pad = narrow ? 16 : 26
      const k = Math.max(0.25, Math.min((width - pad * 2) / gw, (height - pad * 2) / gh, 2))
      const t = d3.zoomIdentity
        .translate(width / 2 - k * (minX + maxX) / 2, height / 2 - k * (minY + maxY) / 2)
        .scale(k)
      const sel = animate ? svg.transition().duration(280) : svg
      ;(sel as any).call(zoom.transform, t)
    }

    // ── 画边 ──────────────────────────────────────────────────────
    const link = root.append('g')
      .attr('class', 'links')
      .selectAll('line')
      .data(simLinks)
      .enter()
      .append('line')
      .attr('stroke', (d: any) => EDGE_COLORS[d.relation_type] || '#30363d')
      .attr('stroke-width', (d: any) => (pathEdges.has(d.id) ? 2.5 : 1.1))
      .attr('stroke-opacity', (d: any) => (path.length === 0 || pathEdges.has(d.id) ? 0.85 : 0.2))
      .attr('stroke-dasharray', (d: any) => {
        if (d.relation_type === 'association') return '5,3'
        if (d.relation_type === 'derivation') return '2,3'
        return 'none'
      })

    // 边标签（只标注路径上的边，避免拥挤）
    const linkLabel = root.append('g')
      .selectAll('text')
      .data(simLinks.filter((d: any) => pathEdges.has(d.id)))
      .enter()
      .append('text')
      .attr('font-size', 8)
      .attr('fill', '#8b949e')
      .attr('text-anchor', 'middle')
      .text((d: any) => d.relation_type)

    // ── 画节点 ────────────────────────────────────────────────────
    const node = root.append('g')
      .attr('class', 'nodes')
      .selectAll('g')
      .data(simNodes)
      .enter()
      .append('g')
      .style('cursor', 'grab')
      .call(d3.drag<any, any>()
        .on('start', (ev, d: any) => {
          if (!ev.active) simulation.alphaTarget(0.3).restart()
          d.fx = d.x; d.fy = d.y
        })
        .on('drag', (ev, d: any) => { d.fx = ev.x; d.fy = ev.y })
        .on('end', (ev, d: any) => {
          if (!ev.active) simulation.alphaTarget(0)
          d.fx = null; d.fy = null
        }) as any)

    node.append('circle')
      .attr('r', (d: any) => radius(d))
      .attr('fill', (d: any) => NODE_COLORS[d.type] || NODE_COLORS.default)
      .attr('fill-opacity', (d: any) => (pathNodes.has(d.id) ? 0.42 : 0.13))
      .attr('stroke', (d: any) => NODE_COLORS[d.type] || NODE_COLORS.default)
      .attr('stroke-width', (d: any) => (pathNodes.has(d.id) ? 2.5 : 1.4))

    // 根因节点加脉冲环
    node.filter((d: any) => pathNodes.has(d.id) && d.id === path[path.length - 1]?.to)
      .append('circle')
      .attr('r', (d: any) => radius(d) + 5)
      .attr('fill', 'none')
      .attr('stroke', '#f85149')
      .attr('stroke-width', 1.5)
      .attr('stroke-dasharray', '3,3')

    node.append('text')
      .attr('text-anchor', 'middle')
      .attr('dy', '-0.15em')
      .attr('font-size', (d: any) => (radius(d) > 14 ? 9 : 8))
      .attr('font-weight', (d: any) => (pathNodes.has(d.id) ? '700' : '400'))
      .attr('fill', (d: any) => NODE_COLORS[d.type] || '#e6edf3')
      .text((d: any) => shortLabel(d.id))

    node.append('text')
      .attr('text-anchor', 'middle')
      .attr('dy', '1.25em')
      .attr('font-size', 7)
      .attr('fill', '#8b949e')
      .text((d: any) => NODE_LABELS[d.type] || d.type)

    node.append('title')
      .text((d: any) => `${d.id}\n${d.name || ''}\n类型: ${d.type}${d.scope ? '\n层级: ' + d.scope : ''}`)

    // ── 更新位置 ──────────────────────────────────────────────────
    simulation.on('tick', () => {
      link
        .attr('x1', (d: any) => d.source.x)
        .attr('y1', (d: any) => d.source.y)
        .attr('x2', (d: any) => d.target.x)
        .attr('y2', (d: any) => d.target.y)

      linkLabel
        .attr('x', (d: any) => (d.source.x + d.target.x) / 2)
        .attr('y', (d: any) => (d.source.y + d.target.y) / 2)

      node.attr('transform', (d: any) => `translate(${d.x},${d.y})`)
    })

    // 布局收敛后自动适应视图；双击可随时重新适应。
    // 兜底定时器：即使 `end` 因为页面节流/后台标签页没触发，也会在 2.3s 后适应一次。
    simulation.on('end', () => fitView())
    const fitTimer = window.setTimeout(() => fitView(false), 2300)
    svg.on('dblclick.zoom', null)               // 关掉 d3 默认的双击放大，改用双击适应
    svg.on('dblclick', () => fitView())

    return () => {
      window.clearTimeout(fitTimer)
      simulation.stop()
    }
  }, [nodes, edges, path, pathNodes, pathEdges])

  if (nodes.length === 0) {
    return (
      <div className="topo-empty">
        <p>🔗 暂无拓扑数据</p>
        <p className="hint">执行 RCA 分析后，拓扑图将显示在此处</p>
      </div>
    )
  }

  return (
    <div className="topo-container" ref={containerRef}>
      <div className="topo-legend">
        <span style={{ color: '#58a6ff' }}>● 实体</span>
        <span style={{ color: '#3fb950' }}>● 指标</span>
        <span className="topo-stat">{nodes.length} 节点 / {edges.length} 边</span>
      </div>

      {path.length > 0 && (
        <div className="topo-path">
          <div className="topo-path-title">根因传播路径</div>
          <div className="topo-path-chain">
            {[path[0].from, ...path.map(p => p.to)].map((id, i) => (
              <span key={i} className="topo-path-seg">
                {i > 0 && <span className="topo-path-arrow">→</span>}
                <code className={i === 0 ? 'topo-path-root' : 'topo-path-node'}>{shortLabel(id)}</code>
              </span>
            ))}
          </div>
          <div className="topo-path-hint">拖拽节点可调整布局 · 滚轮缩放 · 双击适应窗口</div>
        </div>
      )}

      <svg ref={svgRef} className="topo-svg" />
    </div>
  )
}
