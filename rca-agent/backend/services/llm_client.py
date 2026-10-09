# -*- coding: utf-8 -*-
"""
LLM 客户端 — 支持 OpenAI Chat Completions 与 Anthropic Messages。

设计要点：
  · 原生 httpx，不引入 LangChain（依赖少、可控、易调试）
  · 统一返回 LLMResponse（content / reasoning / tool_calls / usage）
  · agnes-2.5-pro 会返回 reasoning_content（思维链），直接作为 Agent 的"推理"记录
  · 主模型失败自动降级到 fallback_model
"""
from __future__ import annotations
import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from .llm_config import llm_config, resolve_api_key, PROVIDERS, CROSS_PROVIDER_MODEL


def cross_provider_candidates(current_provider: str) -> List[tuple]:
    """
    返回可作为容灾的其它 provider 列表（按优先级）。

    典型场景：agnes 额度耗尽返回 403 时，自动切换到 rsxermu666 的
    claude-opus-5-5（按 CROSS_PROVIDER_MODEL 决策）。
    """
    order = ["rsxermu666", "agnes", "deepseek"]
    out: List[tuple] = []
    for name in order:
        if name == current_provider:
            continue
        spec = PROVIDERS.get(name)
        if spec and resolve_api_key(name):
            model = CROSS_PROVIDER_MODEL.get(name, spec["default_model"])
            out.append((name, spec, model))
    return out


def sanitize_proxy_env() -> None:
    """
    清理代理环境变量中 httpx 无法解析的写法。

    httpx 会把 NO_PROXY 里的 `[::1]` 当成 `host:port`，抛
    `InvalidURL: Invalid port: ':1]'`。这里去掉 IPv6 方括号。
    （urllib 不受影响，因此 Prometheus 客户端无需改动。）
    """
    for var in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(var)
        if not raw:
            continue
        cleaned: List[str] = []
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            # 去掉方括号： [::1] -> ::1 ; [2001:db8::1] -> 2001:db8::1
            if item.startswith("[") and item.endswith("]"):
                item = item[1:-1]
            cleaned.append(item)
        os.environ[var] = ",".join(cleaned)


sanitize_proxy_env()


# ── 数据结构 ──────────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]
    raw_arguments: str = ""

    def to_message_part(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.raw_arguments or json.dumps(self.arguments, ensure_ascii=False)},
        }


@dataclass
class LLMResponse:
    content: str = ""
    reasoning: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    model: str = ""
    error: Optional[str] = None

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def total_tokens(self) -> int:
        try:
            return int(self.usage.get("total_tokens", 0) or 0)
        except (TypeError, ValueError):
            return 0


class LLMError(RuntimeError):
    pass


class LLMFatalError(LLMError):
    """不可重试错误（鉴权失败/额度耗尽/参数错误），应直接切换 provider。"""


# 这些 HTTP 状态码重试无意义，应立即降级
_FATAL_STATUS = {400, 401, 402, 403, 404, 413, 422}


# ── 客户端 ────────────────────────────────────────────────────────────────

class LLMClient:
    """异步 LLM 客户端，带重试与模型降级。"""

    def __init__(self, config: Optional[dict] = None):
        self.cfg = config or llm_config()
        if not self.cfg.get("api_key"):
            raise LLMError(
                "No API key for provider '%s'. Set %s or add it to rca-agent/.env"
                % (self.cfg["provider"], self.cfg["provider"].upper() + "_API_KEY")
            )
        self._client: Optional[httpx.AsyncClient] = None

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def __aenter__(self) -> "LLMClient":
        self._client = httpx.AsyncClient(timeout=self.cfg["llm_timeout_s"])
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.cfg["llm_timeout_s"])
        return self._client

    # ── 主入口 ────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        model: Optional[str] = None,
        max_retries: int = 2,
        tool_choice: str = "auto",
        strict_single: bool = False,
    ) -> LLMResponse:
        """
        调用 LLM。

        降级链（依次尝试，任一成功即返回）：
          1. 本 provider 的主模型
          2. 本 provider 的 fallback 模型
          3. 其它可用 provider 的模型（跨 provider 容灾，如额度耗尽时切换）

        `strict_single=True` 时**只试 (provider, model) 这一个组合**，不走任何降级。
        用途：界面上的"测试连接"必须如实回答"**我选的这个 provider/model 能用吗**"。
        否则容灾链会把失败悄悄兜住 —— 实测踩过：测 `rsxermu666`（claude）返回成功，
        但响应里的模型是 `agnes-3.0-flash`，即真正干活的是**另一个 provider**，
        测试因此给出了假绿灯。
        """
        if strict_single:
            attempts: List[tuple] = [(self.cfg["provider"], model or self.cfg["model"])]
        else:
            attempts = [(self.cfg["provider"], model or self.cfg["model"])]
            fb = self.cfg.get("fallback_model")
            if fb and fb != attempts[0][1]:
                attempts.append((self.cfg["provider"], fb))

            # 跨 provider 容灾（每个候选是 (provider, spec, model)）
            for name, spec, cmodel in cross_provider_candidates(self.cfg["provider"]):
                attempts.append((name, cmodel))

        last_err: Optional[str] = None
        tried: List[str] = []

        for pname, m in attempts:
            key = self.cfg["api_key"] if pname == self.cfg["provider"] else resolve_api_key(pname)
            if not key:
                continue
            api = self.cfg["api"] if pname == self.cfg["provider"] else PROVIDERS[pname]["api"]
            base = self.cfg["base_url"] if pname == self.cfg["provider"] else PROVIDERS[pname]["base_url"]

            tried.append("%s/%s" % (pname, m))
            for retry in range(max_retries + 1):
                try:
                    resp = await self._call_with(pname, api, base, key, messages, tools, m, tool_choice)
                    if resp.error is None and not resp.content and not resp.tool_calls:
                        # 空响应：用 reasoning 兜底，避免 Agent 收到空结论
                        if resp.reasoning:
                            resp.content = resp.reasoning
                        else:
                            raise LLMError("模型返回空响应")
                    if len(tried) > 1:
                        resp.reasoning = "[已降级到 %s/%s] " % (pname, m) + (resp.reasoning or "")
                    return resp
                except Exception as e:  # noqa: BLE001
                    last_err = "%s: %s" % (type(e).__name__, e)
                    # 额度/鉴权类错误重试无意义，直接切换 provider
                    if isinstance(e, LLMFatalError):
                        break
                    if retry < max_retries:
                        await asyncio.sleep(1.5 * (retry + 1))
                        continue
                    break

        return LLMResponse(
            error="LLM 调用失败（已尝试 %s）: %s" % (", ".join(tried) or "无可用 provider", last_err),
            model=attempts[0][1] if attempts else "",
        )

    async def _call_with(
        self, provider: str, api: str, base_url: str, api_key: str,
        messages: List[dict], tools: Optional[List[dict]], model: str, tool_choice: str,
    ) -> LLMResponse:
        """按指定 provider 发起一次调用。"""
        if api == "anthropic-messages":
            return await self._call_anthropic(base_url, api_key, messages, tools, model)
        return await self._call_openai(base_url, api_key, messages, tools, model, tool_choice)

    # ── 单次调用 ──────────────────────────────────────────────────────

    async def _call_once(
        self,
        messages: List[dict],
        tools: Optional[List[dict]],
        model: str,
        tool_choice: str,
    ) -> LLMResponse:
        return await self._call_with(
            self.cfg["provider"], self.cfg["api"], self.cfg["base_url"],
            self.cfg["api_key"], messages, tools, model, tool_choice,
        )

    async def _call_openai(
        self, base_url: str, api_key: str,
        messages: List[dict], tools: Optional[List[dict]], model: str, tool_choice: str
    ) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self.cfg.get("temperature", 0.2),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        t0 = time.perf_counter()
        client = self._ensure_client()
        r = await client.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        )
        latency = (time.perf_counter() - t0) * 1000

        if r.status_code >= 400:
            msg = "HTTP %s: %s" % (r.status_code, r.text[:300])
            if r.status_code in _FATAL_STATUS:
                raise LLMFatalError(msg)
            raise LLMError(msg)

        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}

        return LLMResponse(
            content=(msg.get("content") or "").strip(),
            reasoning=(msg.get("reasoning_content") or msg.get("reasoning") or "").strip(),
            tool_calls=self._parse_tool_calls(msg.get("tool_calls")),
            finish_reason=choice.get("finish_reason") or "",
            usage=data.get("usage") or {},
            latency_ms=round(latency, 1),
            model=data.get("model") or model,
        )

    async def _call_anthropic(
        self, base_url: str, api_key: str,
        messages: List[dict], tools: Optional[List[dict]], model: str
    ) -> LLMResponse:
        """Anthropic Messages API：需要把 system 抽出、tools 换格式。"""
        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        conv = self._to_anthropic_messages([m for m in messages if m.get("role") != "system"])

        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": 8192,
            "messages": conv,
            "temperature": self.cfg.get("temperature", 0.2),
        }
        if system_parts:
            payload["system"] = "\n\n".join(str(s) for s in system_parts)
        if tools:
            payload["tools"] = [self._to_anthropic_tool(t) for t in tools]

        t0 = time.perf_counter()
        client = self._ensure_client()
        r = await client.post(
            base_url.rstrip("/") + "/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        latency = (time.perf_counter() - t0) * 1000

        if r.status_code >= 400:
            msg = "HTTP %s: %s" % (r.status_code, r.text[:300])
            if r.status_code in _FATAL_STATUS:
                raise LLMFatalError(msg)
            raise LLMError(msg)

        data = r.json()
        text_parts, reasoning_parts, tool_calls = [], [], []
        for block in data.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype in ("thinking", "redacted_thinking"):
                reasoning_parts.append(block.get("thinking", "") or "(thinking)")
            elif btype == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input") or {},
                    raw_arguments=json.dumps(block.get("input") or {}, ensure_ascii=False),
                ))

        u = data.get("usage") or {}
        usage = {
            "prompt_tokens": u.get("input_tokens", 0),
            "completion_tokens": u.get("output_tokens", 0),
            "total_tokens": (u.get("input_tokens", 0) or 0) + (u.get("output_tokens", 0) or 0),
        }

        return LLMResponse(
            content="\n".join(text_parts).strip(),
            reasoning="\n".join(reasoning_parts).strip(),
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason") or "",
            usage=usage,
            latency_ms=round(latency, 1),
            model=data.get("model") or model,
        )

    # ── 转换辅助 ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_tool_calls(raw: Optional[list]) -> List[ToolCall]:
        out: List[ToolCall] = []
        for i, tc in enumerate(raw or []):
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except json.JSONDecodeError:
                args = {"_raw": args_raw, "_parse_error": True}
            out.append(ToolCall(
                id=tc.get("id") or "call_%d" % i,
                name=fn.get("name") or "",
                arguments=args if isinstance(args, dict) else {"value": args},
                raw_arguments=args_raw if isinstance(args_raw, str) else json.dumps(args_raw, ensure_ascii=False),
            ))
        return out

    @staticmethod
    def _to_anthropic_messages(messages: List[dict]) -> List[dict]:
        """把 OpenAI 风格消息转成 Anthropic 格式。"""
        out: List[dict] = []
        for m in messages:
            role = m.get("role")
            if role == "tool":
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id", ""),
                        "content": str(m.get("content", "")),
                    }],
                })
            elif role == "assistant" and m.get("tool_calls"):
                blocks: List[dict] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    fn = tc.get("function") or {}
                    try:
                        inp = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        inp = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": inp,
                    })
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": role or "user", "content": m.get("content", "") or ""})
        return out

    @staticmethod
    def _to_anthropic_tool(tool: dict) -> dict:
        fn = tool.get("function") or {}
        return {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        }


# ── 便捷函数 ──────────────────────────────────────────────────────────────

async def health_check() -> dict:
    """验证 LLM 连通性（供 /api/agent/health 使用）。"""
    cfg = llm_config()
    if not cfg.get("api_key"):
        return {
            "ok": False,
            "provider": cfg["provider"],
            "model": cfg["model"],
            "error": "no api key",
        }
    try:
        async with LLMClient(cfg) as c:
            r = await c.chat(
                [{"role": "user", "content": "回复两个字：正常"}],
                max_retries=0,
            )
        return {
            "ok": r.error is None,
            "provider": cfg["provider"],
            "model": r.model,
            "latency_ms": r.latency_ms,
            "tokens": r.total_tokens,
            "sample": (r.content or "")[:40],
            "error": r.error,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "provider": cfg["provider"], "model": cfg["model"],
                "error": "%s: %s" % (type(e).__name__, e)}
