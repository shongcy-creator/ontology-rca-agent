# -*- coding: utf-8 -*-
"""
LLM 运行时配置。

API Key 解析优先级：
  1. 显式传入
  2. 环境变量（AGNES_API_KEY / RSXERMU666_API_KEY / DEEPSEEK_API_KEY）
  3. DSH 凭据文件 ~/.dsh/.credentials.yaml 的 refs 段
  4. rca-agent/.env 文件
"""
from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Dict, Optional


# ── Provider 定义 ─────────────────────────────────────────────────────────

PROVIDERS: Dict[str, dict] = {
    "agnes": {
        "api": "openai-completions",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "api_key_env": "AGNES_API_KEY",
        # 决策：直走 agnes-3.0-flash（2.5-pro 额度不稳定）
        "default_model": "agnes-3.0-flash",
        "fallback_model": "agnes-3.0-flash",
    },
    "rsxermu666": {
        "api": "anthropic-messages",
        "base_url": "https://rsxermu666.cn",
        "api_key_env": "RSXERMU666_API_KEY",
        "default_model": "claude-opus-5-5",
        "fallback_model": "claude-sonnet-4-6",
    },
    "deepseek": {
        "api": "openai-completions",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "fallback_model": "deepseek-chat",
    },
}

# 跨 provider 容灾时使用的模型（覆盖 PROVIDERS 的 default_model）。
# 决策：agnes-3.0-flash 不可用 → rsxermu666 的 claude-opus-5-5。
CROSS_PROVIDER_MODEL: Dict[str, str] = {
    "rsxermu666": "claude-opus-5-5",
    "agnes": "agnes-3.0-flash",
    "deepseek": "deepseek-chat",
}


def _parse_dsh_credentials() -> Dict[str, str]:
    """从 DSH 凭据文件解析 refs 段的 key（不依赖 yaml 库）。"""
    out: Dict[str, str] = {}
    candidates = [
        Path(os.environ.get("DSH_CREDENTIALS", "")) if os.environ.get("DSH_CREDENTIALS") else None,
        Path.home() / ".dsh" / ".credentials.yaml",
        Path(os.environ.get("USERPROFILE", "")) / ".dsh" / ".credentials.yaml",
    ]
    for path in candidates:
        if not path or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # 只解析 refs: 段（缩进两空格 + KEY: value）
        in_refs = False
        for line in text.splitlines():
            if re.match(r"^refs:\s*$", line):
                in_refs = True
                continue
            if in_refs:
                if re.match(r"^\S", line):  # 遇到下一个顶层键，结束
                    break
                m = re.match(r"^\s+([A-Z0-9_]+):\s*(\S+)\s*$", line)
                if m:
                    out[m.group(1)] = m.group(2)
        if out:
            break
    return out


def _parse_dotenv() -> Dict[str, str]:
    """解析 rca-agent/.env（KEY=VALUE）。"""
    out: Dict[str, str] = {}
    root = Path(__file__).resolve().parent.parent.parent
    for name in (".env", ".env.local"):
        f = root / name
        if not f.is_file():
            continue
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass
    return out


_MASK = re.compile(r"^(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]+$")


def mask_key(key: Optional[str]) -> str:
    """脱敏显示 API Key。"""
    if not key:
        return "(none)"
    m = _MASK.match(key)
    return m.group(1) + "***" if m else key[:4] + "***"


def resolve_api_key(provider: str, explicit: Optional[str] = None) -> Optional[str]:
    """按优先级解析 API Key。"""
    if explicit:
        return explicit
    # 运行中手动填入的 key（仅内存、不落盘）优先于环境变量 ——
    # 语义是"我现在就要用这个 key"，而不是"永久改配置"。
    rt = (_RUNTIME.get("keys") or {}).get(provider)
    if rt:
        return rt
    spec = PROVIDERS.get(provider)
    if not spec:
        return None
    env_name = spec["api_key_env"]

    for source in (
        lambda: os.environ.get(env_name),
        lambda: _parse_dotenv().get(env_name),
        lambda: _parse_dsh_credentials().get(env_name),
    ):
        try:
            v = source()
            if v:
                return v
        except Exception:
            continue
    return None


# ══════════════════════════════════════════════════════════════════════════
# 运行中手动切换 LLM（界面上的「手动更改 provider」）
# ══════════════════════════════════════════════════════════════════════════
#
# 三条设计取舍（都是刻意的）：
#  · **只放内存、不落盘**：切换是"当前进程立即生效"的运维动作。把 API Key
#    写进磁盘（.env / 配置文件）是另一件事，需要显式操作，不该由一次界面点击
#    悄悄完成。
#  · **进程重启即回到环境变量**（compose 的 RCA_LLM_PROVIDER 等）——
#    这一点必须让使用人知道，否则会以为"改了永久生效"。
#  · **provider 必须来自已注册表**，不允许"任意 base_url + 任意 key"：
#    否则这个接口就成了让服务端向任意地址发请求的 SSRF 通道。
#
# `llm_config()` 每次调用都读这里，所以下一次诊断就生效，无需重启后端
# （Agent / LLMClient 都是按需构造 cfg）。

_RUNTIME: Dict[str, object] = {
    "provider": None,
    "model": None,
    "base_url": None,
    "fallback_model": None,
    "keys": {},          # provider → 明文 key（仅内存）
}


def set_runtime_config(provider: Optional[str] = None,
                       model: Optional[str] = None,
                       base_url: Optional[str] = None,
                       api_key: Optional[str] = None,
                       fallback_model: Optional[str] = None) -> Dict[str, object]:
    """设置运行中覆盖项；返回被设置的字段（供界面显示"哪些是手动改的"）。"""
    changed: Dict[str, object] = {}
    if provider is not None:
        if provider not in PROVIDERS:
            raise ValueError("未注册的 provider: %s（可用：%s）"
                             % (provider, ", ".join(sorted(PROVIDERS))))
        _RUNTIME["provider"] = provider
        changed["provider"] = provider
        # 切 provider 时清掉 model/base_url 的旧覆盖 —— 否则会把 A 的模型名
        # 带到 B 的端点上（例如拿 agnes-3.0-flash 去请求 claude）
        _RUNTIME["model"] = None
        _RUNTIME["base_url"] = None
        _RUNTIME["fallback_model"] = None
    if model is not None:
        _RUNTIME["model"] = model.strip() or None
        changed["model"] = _RUNTIME["model"]
    if base_url is not None:
        _RUNTIME["base_url"] = base_url.strip() or None
        changed["base_url"] = _RUNTIME["base_url"]
    if fallback_model is not None:
        _RUNTIME["fallback_model"] = fallback_model.strip() or None
        changed["fallback_model"] = _RUNTIME["fallback_model"]
    if api_key is not None and str(api_key).strip():
        eff = str(_RUNTIME.get("provider") or os.environ.get("RCA_LLM_PROVIDER", "agnes"))
        keys = _RUNTIME.setdefault("keys", {})
        assert isinstance(keys, dict)
        keys[eff] = str(api_key).strip()
        changed["api_key_provider"] = eff
    return changed


def clear_runtime_config() -> None:
    """清掉全部运行中覆盖（回到环境变量 / compose 的配置）。"""
    _RUNTIME["provider"] = None
    _RUNTIME["model"] = None
    _RUNTIME["base_url"] = None
    _RUNTIME["fallback_model"] = None
    _RUNTIME["keys"] = {}


def runtime_config() -> Dict[str, object]:
    """当前运行中覆盖了哪些字段（供界面显示"手动覆盖中"）。"""
    return {
        "provider": _RUNTIME.get("provider"),
        "model": _RUNTIME.get("model"),
        "base_url": _RUNTIME.get("base_url"),
        "fallback_model": _RUNTIME.get("fallback_model"),
        "key_providers": sorted((_RUNTIME.get("keys") or {}).keys()),
        "active": any([_RUNTIME.get("provider"), _RUNTIME.get("model"),
                       _RUNTIME.get("base_url"), _RUNTIME.get("fallback_model"),
                       bool(_RUNTIME.get("keys"))]),
    }


def llm_config() -> dict:
    """读取 Agent 运行配置（环境变量 → 运行中覆盖 → 兜底默认）。"""
    env_provider = os.environ.get("RCA_LLM_PROVIDER", "agnes")
    provider = str(_RUNTIME.get("provider") or env_provider)
    spec = PROVIDERS.get(provider, PROVIDERS["agnes"])

    def _int(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default

    def _float(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default

    # ⚠ `RCA_LLM_MODEL` / `RCA_LLM_BASE_URL` / `RCA_LLM_FALLBACK_MODEL` 是**为环境变量里
    # 那个 provider 写的**（compose 里写死 `RCA_LLM_MODEL=agnes-3.0-flash`）。
    # 一旦运行中切到**别的** provider，它们就成了错误的值 ——
    # 实测踩过：切到 rsxermu666（claude）后模型仍是 `agnes-3.0-flash`，
    # 等于拿着 A 家的模型名去请求 B 家的端点。
    # 因此：运行中覆盖的 provider 与环境变量 provider **不同**时，忽略这三个环境变量，
    # 改用新 provider 自己的默认值；两者相同时维持原有优先级。
    env_provider_effective = (env_provider == provider)

    def _pick(runtime_key: str, env_name: str, spec_key: str) -> str:
        rt = _RUNTIME.get(runtime_key)
        if rt:
            return str(rt)
        if env_provider_effective and os.environ.get(env_name):
            return str(os.environ[env_name])
        return str(spec[spec_key])

    return {
        "provider": provider,
        "api": spec["api"],
        # base_url / model / fallback_model：运行中覆盖 > (同 provider 时)环境变量 > provider 默认
        "base_url": _pick("base_url", "RCA_LLM_BASE_URL", "base_url"),
        "model": _pick("model", "RCA_LLM_MODEL", "default_model"),
        "fallback_model": _pick("fallback_model", "RCA_LLM_FALLBACK_MODEL", "fallback_model"),
        "api_key": resolve_api_key(provider),
        # 预算上限（Q4 决策：max_steps=8 / 180s / 60k tokens）
        "max_steps": _int("RCA_AGENT_MAX_STEPS", 8),
        "timeout_s": _float("RCA_AGENT_TIMEOUT_S", 180.0),
        "max_tokens": _int("RCA_AGENT_MAX_TOKENS", 60000),
        "llm_timeout_s": _float("RCA_LLM_TIMEOUT_S", 120.0),
        "temperature": _float("RCA_LLM_TEMPERATURE", 0.2),
        # 确定性路由阈值（Q2 决策 A：本体优先）
        "fast_path_confidence": _float("RCA_FAST_PATH_CONFIDENCE", 0.85),
    }


def available_providers() -> Dict[str, dict]:
    """列出可用 provider 及其 key 状态（脱敏）。"""
    out = {}
    for name, spec in PROVIDERS.items():
        key = resolve_api_key(name)
        out[name] = {
            "api": spec["api"],
            "base_url": spec["base_url"],
            "default_model": spec["default_model"],
            "fallback_model": spec["fallback_model"],
            "key_present": bool(key),
            "key_masked": mask_key(key),
        }
    return out
