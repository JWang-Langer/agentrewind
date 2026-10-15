# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""OpenAI 兼容的异步 LLM 客户端（只依赖 httpx，不绑定任何厂商 SDK）。"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from agentrewind.models import LLMRequest, LLMResponse, ToolCall


class LLMClient(Protocol):
    """任何能完成一次补全调用的客户端（结构性类型，便于测试替身与自定义实现）。"""

    async def complete(self, req: LLMRequest) -> LLMResponse: ...

    async def aclose(self) -> None: ...


class LLMHTTPError(RuntimeError):
    """上游返回非 2xx。"""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"LLM HTTP {status_code}: {body[:500]}")
        self.status_code = status_code
        self.body = body


def parse_completion(data: dict[str, Any]) -> LLMResponse:
    """把 /chat/completions 响应解析成规范化对象。"""
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("response has no choices")
    message = choices[0].get("message") or {}
    tool_calls = [
        ToolCall(
            id=str(call.get("id", f"call_{idx}")),
            name=str((call.get("function") or {}).get("name", "")),
            arguments=str((call.get("function") or {}).get("arguments", "{}")),
        )
        for idx, call in enumerate(message.get("tool_calls") or [])
    ]
    usage = data.get("usage") or {}
    return LLMResponse(
        text=message.get("content") or "",
        tool_calls=tool_calls,
        finish_reason=choices[0].get("finish_reason"),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        raw=data,
    )


class OpenAICompatClient:
    """任何提供 /chat/completions 的服务（OpenAI、DeepSeek、vLLM、本地 stub）都能用。"""

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        timeout: float = 120.0,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        merged = {"Content-Type": "application/json"}
        if api_key:
            merged["Authorization"] = f"Bearer {api_key}"
        if headers:
            merged.update(headers)
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=merged, timeout=timeout, transport=transport
        )

    async def complete(self, req: LLMRequest) -> LLMResponse:
        response = await self._client.post("/chat/completions", json=req.to_payload())
        if response.status_code // 100 != 2:
            raise LLMHTTPError(response.status_code, response.text)
        return parse_completion(response.json())

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OpenAICompatClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
