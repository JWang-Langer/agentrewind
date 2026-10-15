# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""本地 stub LLM 服务：真实的 HTTP 服务（不是 mock 替换），供演示与测试使用。

设计要点：每次响应里带一个**随机 nonce**。于是"回放是否真的返回了录制时的响应"
可以被直接观测——回放若拿了新响应，nonce 必然变化。这比断言"函数被调用过"
更能证明确定性的成立。
"""

from __future__ import annotations

import json
import os
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class StubState:
    """跨请求共享的计数（用于证明"回放没有产生上游调用"）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.lock = threading.Lock()

    def bump(self) -> int:
        with self.lock:
            self.calls += 1
            return self.calls


class _Handler(BaseHTTPRequestHandler):
    state: StubState

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
        if not self.path.endswith("/chat/completions"):
            self.send_error(404, "only /chat/completions is served")
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        call_no = type(self).state.bump()
        body = build_completion(payload, call_no)
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: Any) -> None:
        return


def build_completion(payload: dict[str, Any], call_no: int) -> dict[str, Any]:
    """按"是否需要查价格"决定返回工具调用还是最终回答。"""
    messages = payload.get("messages") or []
    contents = [str(m.get("content") or "") for m in messages if isinstance(m, dict)]
    has_tool_result = any(isinstance(m, dict) and m.get("role") == "tool" for m in messages)
    wants_lookup = any("LOOKUP" in text for text in contents)

    nonce = f"{random.randint(0, 10**9):09d}"
    if wants_lookup and not has_tool_result:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{call_no}",
                    "type": "function",
                    "function": {
                        "name": "lookup_price",
                        "arguments": json.dumps({"sku": "SKU-1"}, ensure_ascii=False),
                    },
                }
            ],
        }
        finish_reason = "tool_calls"
        completion_tokens = 17
    else:
        message = {"role": "assistant", "content": f"SKU-1 价格 199.00 元。结论编号[{nonce}]"}
        finish_reason = "stop"
        completion_tokens = 42

    return {
        "id": f"chatcmpl-stub-{call_no}",
        "object": "chat.completion",
        "created": 0,
        "model": payload.get("model", "stub"),
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": 123,
            "completion_tokens": completion_tokens,
            "total_tokens": 165,
        },
    }


def start_stub(port: int | None = None) -> tuple[ThreadingHTTPServer, StubState, str]:
    """启动 stub 服务，返回 (server, state, base_url)。port=0 时由系统分配。"""
    state = StubState()
    handler = type("StubHandler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", port or 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state, f"http://127.0.0.1:{server.server_address[1]}/v1"


if __name__ == "__main__":  # pragma: no cover - 手工起服务
    import contextlib

    _port = int(os.environ.get("STUB_PORT", "0"))
    _srv, _state, _url = start_stub(_port)
    print(f"stub LLM 已启动: {_url}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        _srv.serve_forever()
