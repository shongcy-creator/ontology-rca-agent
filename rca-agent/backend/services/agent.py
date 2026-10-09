# -*- coding: utf-8 -*-
"""
RCA Agent — 标准「观察 → 推理 → 行动」循环。

设计参考 Dify Agent 机制，并做两处关键改进：
  1. 用 Function Calling（结构化 tool_calls）替代 ReAct 文本解析，消除格式漂移
  2. 捕获 reasoning_content（模型原生思维链）作为"推理"记录，可解释性更强

双引擎协同（Q2 决策 A）：
  · 本体确定性结论作为【首个观察值】注入，让 LLM 在正确假设空间里探索
  · 本体结果同样可作为工具（ontology_diagnose）被 LLM 主动咨询
  · 超时/失败时自动回落确定性结论

安全（Q3 决策：只读）：全部工具只读，产出建议由人工执行。
"""
from __future__ import annotations
import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .llm_client import LLMClient, LLMResponse, sanitize_proxy_env
from .llm_config import llm_config
from .tools import build_default_registry, ToolRegistry
from .tools.base import redact

sanitize_proxy_env()


# ── 数据结构 ──────────────────────────────────────────────────────────────

@dataclass
class AgentStep:
    step_no: int
    phase: str                       # observe | reason | act
    reasoning: str = ""
    tool_name: str = ""
    tool_input: Dict[str, Any] = field(default_factory=dict)
    observation: str = ""
    observation_ok: bool = True
    tokens: int = 0
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "step": self.step_no,
            "phase": self.phase,
            "reasoning": self.reasoning,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "observation": self.observation[:1500],
            "observation_ok": self.observation_ok,
            "tokens": self.tokens,
            "latency_ms": self.latency_ms,
        }


@dataclass
class AgentResult:
    run_id: str = ""
    incident_id: str = ""
    mode: str = "deterministic"       # deterministic | agentic
    status: str = "completed"         # completed | timeout | budget_exceeded | failed | fallback
    root_cause: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    evidence: List[dict] = field(default_factory=list)
    affected_entities: List[str] = field(default_factory=list)
    reasoning_summary: str = ""
    next_actions: List[dict] = field(default_factory=list)
    steps: List[AgentStep] = field(default_factory=list)
    steps_used: int = 0
    total_tokens: int = 0
    #: 输入/输出 token（用于按模型精算费用；两者单价差 4~5 倍）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # ── token 预算（硬上限）─────────────────────────────────────────────
    # 原先只在**循环开头**检查预算，于是一次大调用冲过上限、而该次恰好给出
    # 最终答案时，运行仍以 `completed` 收尾 —— 实测出现过"超过 60000 仍是 completed"。
    # 现在**每次调用后**立即校验：超出就记进这两个字段（金额、说明），
    # 并且不再发起下一次调用（真正意义上的硬上限）。
    budget_tokens: int = 0            # 本次生效的预算
    budget_overshoot_tokens: int = 0  # 超出多少（0 = 未超）
    budget_note: str = ""
    latency_ms: float = 0.0
    seed_agreement: Optional[bool] = None   # 与本体确定性结论是否一致
    seed_agreement_level: Optional[str] = None  # exact | category | divergent
    error: str = ""
    # ── 本体拓扑（供前端「拓扑」面板渲染）──────────────────────────────
    # 这两个字段以前**没有**，于是 `/api/agent/diagnose` 的最终事件里根本没有拓扑，
    # 前端 `nodes=[]` → `TopologyGraph` 在 `nodes.length === 0` 时直接 return →
    # 拓扑页永远空白。种子引擎（RCAEngine.infer）其实一直算出了完整拓扑，
    # 只是没被带出来；这里补上，两条路径（deterministic / agentic）都填。
    topology: List[dict] = field(default_factory=list)
    topology_edges: List[dict] = field(default_factory=list)
    root_cause_path: List[dict] = field(default_factory=list)
    # 便于前端展示
    tools_used: List[str] = field(default_factory=list)
    model: str = ""

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "incident_id": self.incident_id,
            "mode": self.mode,
            "status": self.status,
            "root_cause": self.root_cause,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "affected_entities": self.affected_entities,
            "reasoning_summary": self.reasoning_summary,
            "next_actions": self.next_actions,
            "steps": [s.to_dict() for s in self.steps],
            "steps_used": self.steps_used,
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "budget_tokens": self.budget_tokens,
            "budget_overshoot_tokens": self.budget_overshoot_tokens,
            "budget_note": self.budget_note,
            "latency_ms": self.latency_ms,
            "seed_agreement": self.seed_agreement,
            "seed_agreement_level": self.seed_agreement_level,
            "topology": self.topology,
            "topology_edges": self.topology_edges,
            "root_cause_path": self.root_cause_path,
            "tools_used": self.tools_used,
            "model": self.model,
            "error": self.error,
        }


# ── System Prompt ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是信用卡支付系统的资深 SRE，负责生产故障的根因分析（RCA）。

## 系统领域知识（本体已建模）

拓扑分层：
  应用层    : app:payment-app（支付交易应用 v1.8.2）, fn:auth, api:auth
  运行环境层: env:container（容器 cc-credit-card-app）→ env:host-wsl（Docker Desktop WSL2）→
             env:host-phys（物理机「遥遥领先」）
  数据库层  : db:mysql-core（MySQL 8.0, max_connections=200）
             ds:pay（连接池, poolLimit=10）
             table:t_txn（交易表，含 idx_txn_customer / idx_txn_created）
             table:t_customer / table:t_audit
  支撑实体  : metric:mysql-p99, metric:conn-exhaust, alert:p99,
             incident:payment-timeout, rc:slow-sql（已知根因：t_txn 慢查询致连接耗尽）

关键依赖路径：
  payment-app --runsOn--> container --deployedOn--> host-wsl
  payment-app --accesses--> mysql-core --containsTable--> t_txn
  payment-app --hasDataSource--> ds:pay(poolLimit=10) --dataSourceOf--> mysql-core

已知约束（违反即为故障）：
  con:p99-threshold   HTTP P99 必须 < 500ms
  con:pool-exhaust    连接池不得耗尽
  con:db-maxconn      连接数必须 < max_connections

## 工作方式：观察 → 推理 → 行动

每轮你都必须：
1. **观察** 阅读已注入的指标/本体快照与上一轮工具返回
2. **推理** 明确说明你当前的判断依据、待验证的假设、以及为什么下一步要查那个信息
3. **行动** 调用工具获取缺失证据；证据充分时直接给出最终结论（不再调用工具）

## 硬性规则

- **只读诊断**：绝不执行写操作。可以在建议中给出修复命令，但必须标注 needs_approval=true 表示需人工确认。
- **证据优先**：结论必须基于工具实际返回的数据，引用具体数值与来源工具名。禁止臆断。
- **主动取证**：怀疑慢查询/锁等待时，必须查 db_processlist 或 db_innodb_status 拿到直接证据，不要只凭指标猜测。
- **交叉验证**：可调用 ontology_diagnose 获取本体确定性结论作为参照；若你的证据与它不一致，必须说明差异原因并以证据为准。
- **证据不足就说不确定**：明确说明"当前无法确定，需要进一步检查 X"，并给出置信度。
- **效率**：工具按需调用，避免重复查同一信息。通常 3-6 步足够。

## 最终结论输出格式

当你认为证据充分时，直接输出**纯 JSON**（不要包裹 markdown 代码块）：

{
  "root_cause": {
    "category": "数据|资源|配置|依赖|代码",
    "entity_id": "本体实体ID，如 rc:slow-sql / env:container / db:mysql-core",
    "description": "用一两句话说明根因机理"
  },
  "confidence": 0.0到1.0之间的小数,
  "evidence": [
    {"source": "工具名", "finding": "发现了什么", "value": "关键数值"}
  ],
  "affected_entities": ["受影响的本体实体ID"],
  "reasoning_summary": "一句话总结推理链",
  "next_actions": [
    {"urgency": "P0|P1|P2", "action": "具体操作", "command": "可执行命令（可选）", "needs_approval": true}
  ]
}

现在开始分析。
"""


# ── Agent ─────────────────────────────────────────────────────────────────

class RCAAgent:
    def __init__(
        self,
        alert: str,
        severity: str = "P1",
        seed: Optional[dict] = None,
        registry: Optional[ToolRegistry] = None,
        config: Optional[dict] = None,
        on_event: Optional[Callable[[str, dict], None]] = None,
    ):
        self.alert = alert
        self.severity = severity
        self.seed = seed                       # 本体确定性结论（Q2-A 的先验注入）
        self.cfg = config or llm_config()
        self.registry = registry or build_default_registry()
        self.on_event = on_event or (lambda ev, data: None)

        self.run_id = "RUN-" + uuid.uuid4().hex[:12]
        self.steps: List[AgentStep] = []
        self.tools_used: List[str] = []
        self.total_tokens = 0
        #: 输入/输出 token 分开记（按模型精算费用需要）
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._t0 = 0.0
        #: token 预算硬上限（循环里每次调用后校验；超出即停止再调用并标记）
        self.token_budget = int(self.cfg.get("max_tokens") or 0)
        self._budget_overshoot = 0
        self._budget_note = ""

    # ── 事件推送（P2 SSE 复用）────────────────────────────────────────

    def _emit(self, event: str, data: dict) -> None:
        try:
            self.on_event(event, data)
        except Exception:
            pass

    # ── 初始观察（不消耗 LLM 轮次）────────────────────────────────────

    async def _initial_observation(self) -> Dict[str, Any]:
        """并行采集：指标快照 + 目标健康 + 告警状态。本体结论由 seed 提供。"""
        obs: Dict[str, Any] = {}

        async def safe(name: str, args: dict) -> None:
            r = await self.registry.invoke(name, args)
            obs[name] = {"ok": r.ok, "summary": r.summary, "data": r.data}
            self.steps.append(AgentStep(
                step_no=0, phase="observe",
                tool_name=name, tool_input=args,
                observation=r.to_observation(2000),
                observation_ok=r.ok,
                latency_ms=r.meta.get("latency_ms", 0),
            ))

        await asyncio.gather(
            safe("metrics_summary", {}),
            safe("metrics_alerts", {}),
            safe("metrics_targets", {}),
        )
        return obs

    # ── 消息构造 ──────────────────────────────────────────────────────

    def _build_initial_messages(self, obs: Dict[str, Any]) -> List[dict]:
        parts: List[str] = []
        parts.append("## 告警\n\n级别：%s\n描述：%s" % (self.severity, self.alert))

        # 本体确定性结论作为先验
        if self.seed:
            rc = self.seed.get("root_cause") or {}
            parts.append(
                "## 本体确定性引擎的初步结论（先验参考）\n\n"
                "- 根因：%s（类别 %s）\n- 置信度：%s\n- 命中关键词：%s\n"
                "- 候选：%s\n\n"
                "这只是规则引擎的初判，**不要直接采信**。"
                "请用工具验证它，若证据不符请给出你自己的结论并说明差异。" % (
                    rc.get("entity_id", "?"), rc.get("category", "?"),
                    self.seed.get("confidence"),
                    ", ".join((self.seed.get("alert") or {}).get("keywords", [])[:8]) or "无",
                    "; ".join("%s(%s)" % (c.get("entity_id"), c.get("category"))
                              for c in (self.seed.get("candidates") or [])[:4]) or "无",
                ))

        # 自动采集的观测（snippet 精简，控制首轮 prompt 体量）
        parts.append("## 已自动采集的观测\n")
        for name, o in obs.items():
            status = "成功" if o.get("ok") else "失败"
            parts.append("### %s（%s）\n%s" % (name, status, o.get("summary") or "(无摘要)"))
            if o.get("ok") and o.get("data"):
                try:
                    snippet = json.dumps(o["data"], ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    snippet = str(o["data"])
                parts.append("```json\n%s\n```" % snippet[:900])

        parts.append("## 你的任务\n\n请开始观察→推理→行动。需要更多证据时调用工具。")

        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)},
        ]

    # ── 主循环 ────────────────────────────────────────────────────────

    async def run(self) -> AgentResult:
        self._t0 = time.perf_counter()
        max_steps = int(self.cfg["max_steps"])
        timeout_s = float(self.cfg["timeout_s"])
        token_budget = int(self.cfg["max_tokens"])

        self._emit("run_start", {
            "run_id": self.run_id, "alert": self.alert, "severity": self.severity,
            "model": self.cfg["model"], "max_steps": max_steps,
        })

        try:
            async with LLMClient(self.cfg) as llm:
                res = await self._loop(llm, max_steps, timeout_s, token_budget)
        except Exception as e:  # noqa: BLE001
            res = self._fallback("agent 初始化失败: %s: %s" % (type(e).__name__, e))
        # 统一在这里打上预算标记：`_loop` 内部有多条返回路径（最终答案 / 预算耗尽 /
        # 超时 / 回退），逐个改容易漏 —— 而"漏掉标记"正是这个功能要修的问题本身。
        res.budget_tokens = token_budget
        res.budget_overshoot_tokens = self._budget_overshoot
        res.budget_note = self._budget_note
        res.prompt_tokens = self.prompt_tokens
        res.completion_tokens = self.completion_tokens
        return res

    async def _loop(self, llm: LLMClient, max_steps: int,
                    timeout_s: float, token_budget: int) -> AgentResult:
        # ① 初始观察
        self._emit("step_start", {"step": 0, "phase": "observe"})
        obs = await self._initial_observation()
        self._emit("step_end", {
            "step": 0, "phase": "observe",
            "summary": "已采集 %d 项观测" % len(obs),
        })

        messages = self._build_initial_messages(obs)
        self.model_name = ""

        for step in range(1, max_steps + 1):
            # 预算熔断
            elapsed = time.perf_counter() - self._t0
            if elapsed > timeout_s:
                return self._finish_best_effort("timeout", "超时（%.0fs）" % elapsed)
            if self.total_tokens > token_budget:
                return self._finish_best_effort("budget_exceeded",
                                                "token 预算耗尽（%d）" % self.total_tokens)

            # 借鉴 Dify：最后一轮摘掉工具，强制收尾
            active_tools = [] if step == max_steps else self.registry.schemas()
            if step == max_steps:
                messages.append({
                    "role": "user",
                    "content": ("这是最后一轮，已不可再调用工具。"
                                "请基于已获得的全部证据，直接输出最终 JSON 结论。"),
                })

            # ② 推理
            self._emit("step_start", {"step": step, "phase": "reason"})
            resp = await llm.chat(messages, tools=active_tools)
            self.model_name = resp.model or self.model_name

            if resp.error:
                return self._fallback("LLM 调用失败: %s" % resp.error)

            self.total_tokens += resp.total_tokens
            # 输入/输出分开累计 —— 两者单价差 4~5 倍（见 MODEL_PRICE_PER_1M_TOKENS），
            # 只记 total 就没法按模型精算费用（§11.21 第 17 项）。
            try:
                self.prompt_tokens += int((resp.usage or {}).get("prompt_tokens", 0) or 0)
                self.completion_tokens += int((resp.usage or {}).get("completion_tokens", 0) or 0)
            except (TypeError, ValueError):
                pass

            # ── token 预算**硬上限**：每次调用后立即校验 ──────────────────
            # 为什么必须放在这里：预算原先只在循环开头检查，于是一次大调用冲过上限、
            # 而该次恰好就是最终答案时，运行依然记为 `completed`
            #（实测出现过"超过配置的 60000 仍返回 completed"）。
            # 现在：① 超出量记进结果字段；② **不再发起任何后续 LLM 调用**。
            if self.token_budget and self.total_tokens > self.token_budget:
                self._budget_overshoot = self.total_tokens - self.token_budget
                self._budget_note = ("单次调用后即超出 token 预算：已用 %d / 预算 %d"
                                     "（超出 %d）" % (self.total_tokens, self.token_budget,
                                                      self._budget_overshoot))
                if resp.has_tool_calls():
                    # 还要继续调工具 → 直接按预算耗尽收尾，不再花钱
                    return self._finish_best_effort(
                        "budget_exceeded",
                        "token 预算超限（%d > %d，超出 %d）—— 已在单次调用后熔断"
                        % (self.total_tokens, self.token_budget, self._budget_overshoot))
                # 已经是最终答案：照用，但结果必须带上"超预算"标记
                self._emit("budget_overshoot", {
                    "total_tokens": self.total_tokens,
                    "budget_tokens": self.token_budget,
                    "overshoot_tokens": self._budget_overshoot,
                })
            reason_step = AgentStep(
                step_no=step, phase="reason",
                reasoning=resp.reasoning or resp.content[:600],
                tokens=resp.total_tokens,
                latency_ms=resp.latency_ms,
            )
            self.steps.append(reason_step)
            self._emit("reasoning", {
                "step": step,
                "reasoning": reason_step.reasoning[:3000],
                "tokens": resp.total_tokens,
                "latency_ms": resp.latency_ms,
            })

            # ③ 终止判定：无工具调用 → 视为最终答案
            if not resp.has_tool_calls:
                self._emit("step_end", {"step": step, "phase": "reason", "final": True})
                return self._finalize(resp.content, step)

            # ④ 行动：执行工具
            messages.append({
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [tc.to_message_part() for tc in resp.tool_calls],
            })

            for call in resp.tool_calls:
                self._emit("tool_call", {
                    "step": step, "name": call.name, "args": call.arguments,
                })
                result = await self.registry.invoke(call.name, call.arguments)
                self.tools_used.append(call.name)

                observation = result.to_observation()
                self.steps.append(AgentStep(
                    step_no=step, phase="act",
                    tool_name=call.name, tool_input=call.arguments,
                    observation=observation,
                    observation_ok=result.ok,
                    latency_ms=result.meta.get("latency_ms", 0),
                ))
                self._emit("observation", {
                    "step": step, "name": call.name, "ok": result.ok,
                    "summary": redact(result.summary)[:400],
                    "latency_ms": result.meta.get("latency_ms", 0),
                })

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": observation,
                })

            self._emit("step_end", {"step": step, "phase": "act",
                                    "tools": [c.name for c in resp.tool_calls]})

        return self._finish_best_effort("budget_exceeded", "达到最大步数 %d" % max_steps)

    # ── 结论解析 ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        """从模型输出中稳健提取 JSON（容忍 markdown 代码块与前后缀文本）。"""
        if not text:
            return None
        s = text.strip()

        # 去掉 markdown 代码块
        m = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.DOTALL)
        if m:
            s = m.group(1).strip()

        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass

        # 回退：定位最外层花括号
        start = s.find("{")
        if start < 0:
            return None
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except json.JSONDecodeError:
                        return None
        return None

    def _finalize(self, content: str, steps_used: int) -> AgentResult:
        parsed = self._extract_json(content)

        if not parsed:
            # 模型给了自然语言结论而非 JSON：保留文本，置信度保守
            return AgentResult(
                run_id=self.run_id,
                incident_id="INC-" + self.run_id.split("-")[-1],
                mode="agentic",
                status="completed",
                root_cause={
                    "category": (self.seed or {}).get("root_cause", {}).get("category", "未知"),
                    "entity_id": (self.seed or {}).get("root_cause", {}).get("entity_id", ""),
                    "description": content[:600],
                },
                confidence=float((self.seed or {}).get("confidence") or 0.5) * 0.8,
                evidence=[],
                reasoning_summary="模型未返回结构化 JSON，已保留自然语言结论，置信度下调",
                steps=self.steps,
                steps_used=steps_used,
                total_tokens=self.total_tokens,
                latency_ms=round((time.perf_counter() - self._t0) * 1000, 1),
                tools_used=sorted(set(self.tools_used)),
                model=self.model_name,
            )

        rc = parsed.get("root_cause") or {}
        try:
            conf = float(parsed.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))

        return AgentResult(
            run_id=self.run_id,
            incident_id="INC-" + self.run_id.split("-")[-1],
            mode="agentic",
            status="completed",
            root_cause=rc if isinstance(rc, dict) else {"description": str(rc)},
            confidence=conf,
            evidence=parsed.get("evidence") or [],
            affected_entities=parsed.get("affected_entities") or [],
            reasoning_summary=parsed.get("reasoning_summary") or "",
            next_actions=parsed.get("next_actions") or [],
            steps=self.steps,
            steps_used=steps_used,
            total_tokens=self.total_tokens,
            latency_ms=round((time.perf_counter() - self._t0) * 1000, 1),
            seed_agreement=self._check_seed_agreement(rc),
            tools_used=sorted(set(self.tools_used)),
            model=self.model_name,
        )
    def _check_seed_agreement(self, agent_rc: dict) -> Optional[bool]:
        """
        对比 Agent 结论与本体确定性结论（交叉验证）。

        粒度：
          exact     实体 ID 完全一致
          category  实体不同但根因类别一致（如 rc:slow-sql vs table:t_txn 同属"数据"）
          divergent 类别也不同

        seed_agreement 仅在 exact/category 时为 True；具体粒度记入
        self.seed_agreement_level 供前端与审计使用。
        """
        self.seed_agreement_level = None
        if not self.seed:
            return None
        seed_rc = (self.seed.get("root_cause") or {})
        if not seed_rc or not agent_rc:
            return None

        s_eid, a_eid = str(seed_rc.get("entity_id") or ""), str(agent_rc.get("entity_id") or "")
        s_cat, a_cat = str(seed_rc.get("category") or ""), str(agent_rc.get("category") or "")

        if s_eid and a_eid and s_eid == a_eid:
            self.seed_agreement_level = "exact"
            return True
        if s_cat and a_cat and s_cat == a_cat:
            self.seed_agreement_level = "category"
            return True
        if s_eid or a_eid or s_cat or a_cat:
            self.seed_agreement_level = "divergent"
            return False
        return None

    def _finish_best_effort(self, status: str, reason: str) -> AgentResult:
        """未完成时，用已收集的证据 + 本体结论给出最优可用结果。"""
        seed_rc = (self.seed or {}).get("root_cause") or {}
        # 从工具观察里抽取证据
        evidence = []
        for s in self.steps:
            if s.phase == "act" and s.observation_ok and s.observation:
                first = s.observation.strip().splitlines()
                if first:
                    evidence.append({"source": s.tool_name, "finding": first[0][:300], "value": ""})

        return AgentResult(
            run_id=self.run_id,
            incident_id="INC-" + self.run_id.split("-")[-1],
            mode="agentic",
            status=status,
            root_cause=dict(seed_rc) if seed_rc else {},
            confidence=float((self.seed or {}).get("confidence") or 0.3) * 0.7,
            evidence=evidence[-8:],
            reasoning_summary="Agent 未在预算内得出结论（%s），已回落至本体确定性结论" % reason,
            steps=self.steps,
            steps_used=max((s.step_no for s in self.steps), default=0),
            total_tokens=self.total_tokens,
            latency_ms=round((time.perf_counter() - self._t0) * 1000, 1),
            seed_agreement=self._check_seed_agreement(seed_rc) if seed_rc else None,
            seed_agreement_level=getattr(self, "seed_agreement_level", None),
            error=reason,
            tools_used=sorted(set(self.tools_used)),
            model=getattr(self, "model_name", ""),
        )

    def _fallback(self, error: str) -> AgentResult:
        """LLM 不可用：完全回落到确定性引擎（Q7 降级路径）。"""
        seed_rc = (self.seed or {}).get("root_cause") or {}
        return AgentResult(
            run_id=self.run_id,
            incident_id="INC-" + self.run_id.split("-")[-1],
            mode="deterministic",
            status="fallback",
            root_cause=dict(seed_rc) if seed_rc else {},
            confidence=float((self.seed or {}).get("confidence") or 0.0),
            evidence=[],
            reasoning_summary="LLM 不可用，已回落至本体确定性引擎",
            steps=self.steps,
            steps_used=max((s.step_no for s in self.steps), default=0),
            total_tokens=self.total_tokens,
            latency_ms=round((time.perf_counter() - self._t0) * 1000, 1),
            error=error,
            tools_used=sorted(set(self.tools_used)),
        )
