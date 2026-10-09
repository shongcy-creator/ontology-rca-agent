# -*- coding: utf-8 -*-
"""
工具注册表。

职责：
  · 注册 / 查询工具
  · 输出 OpenAI function-calling schema
  · 统一执行入口（校验 → 执行 → 计时 → 脱敏 → 包装异常）
"""
from __future__ import annotations
import asyncio
import inspect
import time
from typing import Any, Dict, List, Optional

from .base import (
    ToolResult, ToolSpec,
    ToolSecurityError, check_injection, validate_arguments, redact,
)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}
        self._stats: Dict[str, Dict[str, Any]] = {}

    # ── 注册 ──────────────────────────────────────────────────────────

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError("duplicate tool: %s" % spec.name)
        self._tools[spec.name] = spec
        self._stats[spec.name] = {"calls": 0, "errors": 0, "total_ms": 0.0}

    def register_all(self, specs: List[ToolSpec]) -> None:
        for s in specs:
            self.register(s)

    # ── 查询 ──────────────────────────────────────────────────────────

    def names(self) -> List[str]:
        return sorted(self._tools.keys())

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def by_layer(self, layer: str) -> List[ToolSpec]:
        return [s for s in self._tools.values() if s.layer == layer]

    def layers(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for s in self._tools.values():
            out.setdefault(s.layer, []).append(s.name)
        return {k: sorted(v) for k, v in sorted(out.items())}

    def schemas(self, names: Optional[List[str]] = None) -> List[dict]:
        """OpenAI function-calling 工具定义。"""
        pool = self._tools.values() if names is None else [
            self._tools[n] for n in names if n in self._tools
        ]
        return [s.to_openai_schema() for s in pool]

    def catalog(self) -> List[dict]:
        """给前端展示的工具清单。"""
        return [
            {
                "name": s.name,
                "description": s.description,
                "layer": s.layer,
                "read_only": s.read_only,
                "params": sorted((s.parameters.get("properties") or {}).keys()),
            }
            for s in sorted(self._tools.values(), key=lambda x: (x.layer, x.name))
        ]

    def stats(self) -> Dict[str, Any]:
        return {
            "total_tools": len(self._tools),
            "layers": {k: len(v) for k, v in self.layers().items()},
            "calls": {k: v for k, v in self._stats.items() if v["calls"] > 0},
        }

    # ── 执行 ──────────────────────────────────────────────────────────

    async def invoke(self, name: str, arguments: Optional[dict]) -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            # 借鉴 Dify：工具不存在不抛异常，返回可读错误让模型自我纠正
            return ToolResult.fail(
                "工具 '%s' 不存在。可用工具: %s" % (name, ", ".join(self.names()))
            )

        args = arguments or {}
        t0 = time.perf_counter()

        try:
            # 1. 安全校验
            check_injection(args, allow_keys=spec.injection_exempt)
            clean = validate_arguments(spec, args)

            # 2. 执行（支持同步/异步 handler）
            result = spec.handler(clean)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=spec.timeout_s)
            if not isinstance(result, ToolResult):
                result = ToolResult(ok=True, summary="执行完成", data=result)

        except asyncio.TimeoutError:
            result = ToolResult.fail("执行超时（>%.0fs）" % spec.timeout_s)
        except ToolSecurityError as e:
            result = ToolResult.fail("参数校验失败: %s" % e, summary=str(e))
        except Exception as e:  # noqa: BLE001 — 工具异常不应中断 Agent 循环
            result = ToolResult.fail("%s: %s" % (type(e).__name__, redact(str(e))))

        dt = (time.perf_counter() - t0) * 1000
        st = self._stats.setdefault(name, {"calls": 0, "errors": 0, "total_ms": 0.0})
        st["calls"] += 1
        st["total_ms"] += dt
        if not result.ok:
            st["errors"] += 1

        result.meta.update({
            "tool": name,
            "layer": spec.layer,
            "latency_ms": round(dt, 1),
            "read_only": spec.read_only,
        })
        return result


# ── 全局注册表 ────────────────────────────────────────────────────────────

registry = ToolRegistry()


def build_default_registry() -> ToolRegistry:
    """构建内置工具集（本体/监控/环境/知识 四层）。"""
    from .ontology_tools import ONTOLOGY_TOOLS
    from .metrics_tools import METRICS_TOOLS
    from .db_tools import DB_TOOLS
    from .env_tools import ENV_TOOLS
    from .history_tools import HISTORY_TOOLS

    reg = ToolRegistry()
    reg.register_all(ONTOLOGY_TOOLS)
    reg.register_all(METRICS_TOOLS)
    reg.register_all(DB_TOOLS)
    reg.register_all(ENV_TOOLS)
    reg.register_all(HISTORY_TOOLS)
    return reg
