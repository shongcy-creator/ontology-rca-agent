#!/usr/bin/env python3
"""Implementation for the bird.agent.llm_providers module."""

import asyncio
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import httpx


class LLMProvider(ABC):
    """Implementation of LLMProvider."""

    @abstractmethod
    async def generate_response_with_tools(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Generate response with tools."""
        ...

    @abstractmethod
    def get_provider_name(self) -> str:
        ...


class OpenAIProvider(LLMProvider):
    """Implementation of OpenAIProvider."""

    def __init__(self, api_key: str, model: str = "gpt-4o",
                 base_url: str = "", temperature: float = 0.0):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.temperature = temperature

        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError("The openai package is required: pip install openai")

        verify_ssl = os.getenv("OPENAI_SSL_VERIFY", "true").lower() not in ("false", "0", "no", "off")
        http_client = httpx.AsyncClient(verify=verify_ssl, trust_env=False)

        client_kwargs = {"api_key": api_key, "http_client": http_client}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = AsyncOpenAI(**client_kwargs)

    def get_provider_name(self) -> str:
        return f"openai/{self.model}"

    async def generate_response_with_tools(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        openai_tools = [{
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}, "required": []}),
            },
        } for t in tools]

        last_error = None
        for attempt in range(3):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=openai_tools or None,
                    temperature=self.temperature,
                    timeout=120,
                )

                choice = response.choices[0]
                message = choice.message

                result = {
                    "content": message.content or "",
                    "tool_calls": [],
                    "finish": False,
                }

                if message.tool_calls:
                    for tc in message.tool_calls:
                        result["tool_calls"].append({
                            "id": tc.id,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        })


                if message.content:
                    import re
                    if re.search(r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?FINISH\s*:",
                                 message.content):
                        result["finish"] = True


                if hasattr(response, "usage") and response.usage:
                    result["usage"] = {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "total_tokens": response.usage.total_tokens,
                    }

                return result

            except Exception as e:
                last_error = e
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

        raise RuntimeError(
            f"LLM request failed after 3 attempts: {last_error}"
        ) from last_error


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek API provider (OpenAI-compatible)。"""

    def __init__(self, api_key: str, model: str = "deepseek-chat",
                 base_url: str = "https://api.deepseek.com/v1",
                 temperature: float = 0.0):
        super().__init__(api_key=api_key, model=model, base_url=base_url,
                         temperature=temperature)

    def get_provider_name(self) -> str:
        return f"deepseek/{self.model}"


def create_llm_provider(provider_type: str, **kwargs) -> LLMProvider:
    """Create llm provider."""
    provider_lower = provider_type.lower()

    if provider_lower == "openai":
        api_key = kwargs.get("api_key") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY or --api-key is required")
        return OpenAIProvider(
            api_key=api_key,
            model=kwargs.get("model", "gpt-4o"),
            base_url=kwargs.get("base_url", ""),
            temperature=kwargs.get("temperature", 0.0),
        )

    elif provider_lower == "deepseek":
        api_key = kwargs.get("api_key") or os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY or --api-key is required")
        return DeepSeekProvider(
            api_key=api_key,
            model=kwargs.get("model", "deepseek-chat"),
            base_url=kwargs.get("base_url", "https://api.deepseek.com/v1"),
            temperature=kwargs.get("temperature", 0.0),
        )

    elif provider_lower in ("vllm", "custom"):
        api_key = kwargs.get("api_key") or os.getenv("VLLM_API_KEY") or "EMPTY"
        return OpenAIProvider(
            api_key=api_key,
            model=kwargs.get("model", ""),
            base_url=kwargs.get("base_url", ""),
            temperature=kwargs.get("temperature", 0.0),
        )

    else:
        raise ValueError(
            f"Unsupported provider: {provider_type}. Supported providers: openai, deepseek, vllm"
        )
