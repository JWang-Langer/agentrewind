# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""副作用自动拦截层：把 httpx 请求透明地接入确定性通道。

动机：要求 Agent 作者为每个外部调用手写 `session.effect(...)` 是可靠但啰嗦的。
本模块提供一个 `httpx.AsyncBaseTransport`，把 HTTP 请求与响应自动登记为副作用：

- 键 = `METHOD URL body-sha256`（**含请求体**，否则不同载荷会误命中）；
- 录制时：真实发出请求，把响应（状态码 / 头 / body）登记为副作用值；
- 回放时：命中同一键则**不发出请求**，直接用录制值重建响应，字节一致。

与 LLM 调用的关系：LLM 调用由 `session.complete()` 走指纹通道，因此调用方应把
上游 LLM 的 host 放进 `passthrough_hosts`，避免同一请求被登记两遍。

已知边界（v0.1，不夸大）：
- 响应会被**完整缓冲**（`await response.aread()`），因此不支持流式副作用响应；
- 拦截的是"请求-响应"这一层，无法感知同一 HTTP 调用在服务端产生的业务副作用
  （例如重复下单）——回放时请求根本不发出，这正是确定性所需要的。
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterable
from typing import Any

import httpx

from agentrewind.recorder import Session

EFFECT_NAME = "http"

# 这些头由 httpx 依据实际 body 重新计算，保留会导致双重解码/长度不符
_DROP_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "connection"}


def effect_key(method: str, url: httpx.URL | str, body: bytes = b"") -> str:
    """副作用幂等键：方法 + 完整 URL + 请求体哈希。"""
    digest = hashlib.sha256(body).hexdigest()[:32]
    return f"{method.upper()} {url} body-sha256={digest}"


def _request_body(request: httpx.Request) -> bytes:
    content = request.content
    return content if isinstance(content, bytes) else bytes(content)


class SideEffectTransport(httpx.AsyncBaseTransport):
    """把所有（未被排除的）HTTP 请求登记为副作用的传输层。"""

    def __init__(
        self,
        session: Session,
        *,
        inner: httpx.AsyncBaseTransport | None = None,
        passthrough_hosts: Iterable[str] = (),
    ) -> None:
        self._session = session
        self._inner: httpx.AsyncBaseTransport = inner or httpx.AsyncHTTPTransport()
        self._passthrough = {host.lower() for host in passthrough_hosts}

    def _is_passthrough(self, request: httpx.Request) -> bool:
        host = request.url.host or ""
        return host.lower() in self._passthrough

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._is_passthrough(request):
            return await self._inner.handle_async_request(request)

        key = effect_key(request.method, request.url, _request_body(request))

        async def producer() -> dict[str, Any]:
            response = await self._inner.handle_async_request(request)
            try:
                await response.aread()
                payload = {
                    "status": response.status_code,
                    "headers": {
                        name: value
                        for name, value in response.headers.items()
                        if name.lower() not in _DROP_HEADERS
                    },
                    "body_b64": base64.b64encode(response.content).decode("ascii"),
                }
            finally:
                await response.aclose()
            return payload

        recorded = await self._session.effect(EFFECT_NAME, key, producer)
        return httpx.Response(
            status_code=recorded["status"],
            headers=recorded["headers"],
            content=base64.b64decode(recorded["body_b64"]),
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def instrument(
    session: Session,
    *,
    passthrough_hosts: Iterable[str] = (),
    inner: httpx.AsyncBaseTransport | None = None,
    **client_kwargs: Any,
) -> httpx.AsyncClient:
    """返回一个挂了拦截传输层的 `httpx.AsyncClient`。

    用法::

        async with instrument(session, passthrough_hosts=["api.deepseek.com"]) as http:
            resp = await http.get("https://quote.example.com/price?sku=SKU-1")
    """
    transport = SideEffectTransport(session, inner=inner, passthrough_hosts=passthrough_hosts)
    return httpx.AsyncClient(transport=transport, **client_kwargs)
