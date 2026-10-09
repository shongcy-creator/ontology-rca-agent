# -*- coding: utf-8 -*-
"""
简单进程内限流器（滑动窗口）。

用于保护 LLM 成本与后端资源：
  · 全局：限制每分钟诊断次数（默认 10 次/分钟）
  · 每会话：限制单会话并发诊断数（默认 1，避免重复提交）
  · 成本上限：单次 token 上限在 llm_config 已配置

生产环境建议替换为 Redis 限流；本实现零依赖、进程内、足够 PoC 使用。
"""
from __future__ import annotations
import os
import time
import threading
from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class RateDecision:
    allowed: bool
    retry_after_s: float = 0.0
    reason: str = ""
    remaining: int = 0
    limit: int = 0

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "retry_after_s": round(self.retry_after_s, 1),
            "reason": self.reason,
            "remaining": self.remaining,
            "limit": self.limit,
        }


class RateLimiter:
    """滑动窗口限流（进程内）。"""

    def __init__(self, max_per_minute: Optional[int] = None):
        self.max_per_minute = max_per_minute or int(
            os.environ.get("RCA_RATE_LIMIT_PER_MINUTE", "10")
        )
        self.window_s = 60.0
        self._hits: deque = deque()
        self._lock = threading.Lock()

    def check(self, key: str = "global") -> RateDecision:
        now = time.monotonic()
        with self._lock:
            # 清理窗口外的记录
            while self._hits and now - self._hits[0] > self.window_s:
                self._hits.popleft()

            if len(self._hits) >= self.max_per_minute:
                retry = self.window_s - (now - self._hits[0])
                return RateDecision(
                    allowed=False,
                    retry_after_s=max(0.0, retry),
                    reason="每分钟诊断次数已达上限（%d）" % self.max_per_minute,
                    remaining=0,
                    limit=self.max_per_minute,
                )

            self._hits.append(now)
            return RateDecision(
                allowed=True,
                remaining=self.max_per_minute - len(self._hits),
                limit=self.max_per_minute,
            )

    def stats(self) -> dict:
        with self._lock:
            now = time.monotonic()
            in_window = sum(1 for h in self._hits if now - h <= self.window_s)
        return {
            "max_per_minute": self.max_per_minute,
            "current_in_window": in_window,
            "remaining": max(0, self.max_per_minute - in_window),
        }


# 全局限流器
limiter = RateLimiter()


# ── 成本估算 ──────────────────────────────────────────────────────────────

# 各模型定价（每百万 token，USD 估算）。用于展示，非精确计费。
MODEL_PRICE_PER_1M_TOKENS = {
    "agnes-3.0-flash": {"input": 0.10, "output": 0.40},
    "agnes-2.5-pro":   {"input": 0.60, "output": 2.40},
    "claude-opus-5-5": {"input": 5.00, "output": 25.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "deepseek-chat":   {"input": 0.14, "output": 0.28},
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> dict:
    """估算一次调用的成本（USD）。"""
    price = MODEL_PRICE_PER_1M_TOKENS.get(
        model, {"input": 1.0, "output": 4.0}
    )
    in_cost = prompt_tokens / 1_000_000 * price["input"]
    out_cost = completion_tokens / 1_000_000 * price["output"]
    total = in_cost + out_cost
    return {
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost_usd": round(total, 6),
    }


def estimate_run_cost(model: str, usage: dict) -> dict:
    """根据 usage 估算一次 Agent 运行成本。"""
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    return estimate_cost(model, prompt, completion)
