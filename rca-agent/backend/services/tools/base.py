# -*- coding: utf-8 -*-
"""
工具基础设施：结果封装、规格定义、安全校验。

安全策略（Q3 决策：纯只读）：
  · 所有工具 read_only=True
  · 参数拒绝 shell 元字符，防止命令注入
  · 输出统一脱敏（密码/token/key）
  · 观察文本按长度截断，避免撑爆上下文
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# ── 安全常量 ──────────────────────────────────────────────────────────────

# shell 元字符：出现即拒绝（工具不接受需要转义的原始命令）
_SHELL_META = re.compile(r"[;&|`$><\n\r]|\$\(|\|\|")

# 脱敏模式
_REDACT_PATTERNS = [
    (re.compile(r"(?i)(password|passwd|pwd)\s*[=:]\s*\S+"), r"\1=***"),
    (re.compile(r"(?i)(api[_-]?key|apikey|secret|token)\s*[=:]\s*\S+"), r"\1=***"),
    (re.compile(r"(?i)authorization:\s*\S+"), "Authorization: ***"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)(-p)\S{4,}"), r"\1***"),  # mysql -pXXXX
]

# 观察文本默认上限（字符）
# 控制 Agent 上下文体量：单条工具观察不超过该长度，
# 避免一次排查消耗过多 token（实测 4000 会导致单轮 >15k tokens）。
DEFAULT_OBSERVATION_LIMIT = 1600


class ToolSecurityError(ValueError):
    """参数未通过安全校验。"""


def redact(text: str) -> str:
    """脱敏敏感信息。"""
    if not text:
        return ""
    out = str(text)
    for pat, repl in _REDACT_PATTERNS:
        out = pat.sub(repl, out)
    return out


def check_injection(args: Dict[str, Any], allow_keys: Optional[List[str]] = None) -> None:
    """
    校验参数中不含 shell 元字符。

    例外：显式允许的键（如 already-validated PromQL / SQL 片段）仍需单独校验，
    此处仅做通用拦截。若调用方确认某字段安全，可通过 allow_keys 跳过。
    """
    allow = set(allow_keys or [])
    for k, v in (args or {}).items():
        if k in allow:
            continue
        values = v if isinstance(v, list) else [v]
        for item in values:
            if isinstance(item, str) and _SHELL_META.search(item):
                raise ToolSecurityError(
                    "参数 '%s' 含非法字符（shell 元字符），已拒绝执行" % k
                )


def validate_arguments(spec: "ToolSpec", args: Dict[str, Any]) -> Dict[str, Any]:
    """按 JSON-Schema 校验并填充默认值。"""
    if not isinstance(args, dict):
        raise ToolSecurityError("工具参数必须是对象")

    schema = spec.parameters or {}
    props: Dict[str, dict] = schema.get("properties") or {}
    required: List[str] = schema.get("required") or []

    # 未知参数直接拒绝（防止模型臆造参数）
    unknown = [k for k in args if k not in props]
    if unknown:
        raise ToolSecurityError(
            "未知参数: %s（可用: %s）" % (", ".join(unknown), ", ".join(props.keys()))
        )

    out: Dict[str, Any] = {}
    for name, pspec in props.items():
        if name in args and args[name] is not None:
            val = args[name]
            ptype = pspec.get("type")
            # 宽松类型转换
            if ptype == "integer" and isinstance(val, str) and val.isdigit():
                val = int(val)
            elif ptype == "number" and isinstance(val, str):
                try:
                    val = float(val)
                except ValueError:
                    pass
            elif ptype == "string" and not isinstance(val, str):
                val = str(val)
            out[name] = val
        elif "default" in pspec:
            out[name] = pspec["default"]
        elif name in required:
            raise ToolSecurityError("缺少必需参数: %s" % name)

    return out


# ── 结果封装 ──────────────────────────────────────────────────────────────

@dataclass
class ToolResult:
    ok: bool
    summary: str = ""              # 给 LLM 的结论摘要（简明）
    data: Any = None               # 结构化数据（给前端/落库）
    error: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_observation(self, limit: int = DEFAULT_OBSERVATION_LIMIT) -> str:
        """渲染为喂给 LLM 的观察文本。"""
        if not self.ok:
            return "[工具执行失败] %s" % redact(self.error or self.summary)

        parts = [redact(self.summary)] if self.summary else []
        if self.data is not None:
            import json
            try:
                rendered = json.dumps(self.data, ensure_ascii=False, default=str, indent=1)
            except (TypeError, ValueError):
                rendered = str(self.data)
            if len(rendered) > limit:
                rendered = rendered[:limit] + "\n... (输出已截断)"
            parts.append(rendered)
        text = "\n".join(parts) if parts else "(无输出)"
        self.meta["observation_chars"] = len(text)
        return text

    @classmethod
    def fail(cls, error: str, summary: str = "") -> "ToolResult":
        return cls(ok=False, summary=summary, error=error)


# ── 工具规格 ──────────────────────────────────────────────────────────────

@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[..., Any]
    layer: str = "misc"             # ontology | metrics | env | knowledge
    read_only: bool = True
    timeout_s: float = 20.0
    # 允许含 shell 元字符的参数名（例如 PromQL 里合法出现 $ 或 |）
    injection_exempt: List[str] = field(default_factory=list)

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
