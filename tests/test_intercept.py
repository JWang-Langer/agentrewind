# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""副作用自动拦截层测试。

断言的是行为契约：
1. 录制时真实发出请求并登记副作用；
2. 回放时**不发出请求**（外部服务计数不变）且返回体与录制逐字节一致；
3. 请求体变化 → 键变化 → 不会误命中（回放应报 effect_miss）；
4. 声明为 passthrough 的 host 不进副作用通道。
"""

from __future__ import annotations

import asyncio
import json
import random
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

import tests  # noqa: F401
from agentrewind.intercept import EFFECT_NAME, effect_key, instrument
from agentrewind.recorder import Divergence, Recorder, Replayer
from agentrewind.store import Store


class _EchoState:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_keys: list[str] = []
        self.lock = threading.Lock()

    def bump(self, key: str) -> int:
        with self.lock:
            self.calls += 1
            self.seen_keys.append(key)
            return self.calls


class _EchoHandler(BaseHTTPRequestHandler):
    state: _EchoState

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        key = f"{self.command} {self.path} {body.decode('utf-8', 'replace')}"
        call_no = type(self).state.bump(key)
        payload = {
            "call_no": call_no,
            # 随机 nonce：如果回放偷偷重新请求，这个值必然变化
            "nonce": f"{random.randint(0, 10**9):09d}",
            "echo_path": self.path,
            "echo_body": body.decode("utf-8", "replace"),
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, *args: Any) -> None:
        return


def _start_echo() -> tuple[ThreadingHTTPServer, _EchoState, str]:
    state = _EchoState()
    handler = type("EchoHandler", (_EchoHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state, f"http://127.0.0.1:{server.server_address[1]}"


FETCH_ARTIFACTS: list[str] = []


async def quote_agent(session: Any, base_url: str, sku: str = "SKU-1") -> dict:
    """一个"外部依赖靠 HTTP"的 Agent：不手写 session.effect，全靠拦截层。"""
    async with instrument(session, passthrough_hosts=[]) as http:
        first = await http.get(f"{base_url}/price?sku={sku}")
        FETCH_ARTIFACTS.append(first.text)
        second = await http.post(f"{base_url}/order", json={"sku": sku, "qty": 2})
        return {"first": first.json(), "second": second.json()}


class _NullClient:
    """本测试的 Agent 不调用 LLM，因此传入一个不会被使用的客户端。"""

    async def complete(self, req: Any) -> Any:  # pragma: no cover - 不应被调用
        raise AssertionError("本测试的 Agent 不应调用 LLM")

    async def aclose(self) -> None:  # pragma: no cover
        return


class InterceptTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "arw.db")
        self.server, self.state, self.base_url = _start_echo()
        FETCH_ARTIFACTS.clear()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def _record(self, **kwargs: Any) -> tuple[Any, Any, Any]:
        run = self.store.create_run("intercept-record", mode="record")
        session = Recorder(self.store, run, client=_NullClient())
        result = asyncio.run(quote_agent(session, self.base_url))
        self.store.finish_run(run.id)
        return run, session, result

    def _replay(
        self, source_run_id: str, on_divergence: str = "raise", sku: str = "SKU-1"
    ) -> tuple[Any, Any, Any, Exception | None]:
        run = self.store.create_run("intercept-replay", mode="replay", parent_run_id=source_run_id)
        session = Replayer(
            self.store, run, source_run_id=source_run_id, client=None, on_divergence=on_divergence
        )
        error: Exception | None = None
        try:
            result = asyncio.run(quote_agent(session, self.base_url, sku=sku))
        except Divergence as exc:
            result, error = None, exc
        self.store.finish_run(run.id, "diverged" if error else "finished")
        return run, session, result, error


class _NullClient:
    """本测试的 Agent 不调用 LLM，因此传入一个不会被使用的客户端。"""

    async def complete(self, req: Any) -> Any:  # pragma: no cover - 不应被调用
        raise AssertionError("本测试的 Agent 不应调用 LLM")

    async def aclose(self) -> None:  # pragma: no cover
        return


class _NullClient:
    """本测试的 Agent 不调用 LLM，因此传入一个不会被使用的客户端。"""

    async def complete(self, req: Any) -> Any:  # pragma: no cover - 不应被调用
        raise AssertionError("本测试的 Agent 不应调用 LLM")

    async def aclose(self) -> None:  # pragma: no cover
        return


class TestEffectKey(unittest.TestCase):
    def test_key_includes_method_url_and_body(self) -> None:
        a = effect_key("GET", "http://h/p", b"")
        b = effect_key("POST", "http://h/p", b"")
        c = effect_key("POST", "http://h/p", b'{"a":1}')
        d = effect_key("POST", "http://h/p", b'{"a":2}')
        self.assertEqual(len({a, b, c, d}), 4)

    def test_same_input_same_key(self) -> None:
        self.assertEqual(
            effect_key("POST", "http://h/p", b"x"), effect_key("post", "http://h/p", b"x")
        )


class TestRecordAndReplayHttpSideEffects(InterceptTestCase):
    def test_record_registers_two_effects_and_hits_upstream(self) -> None:
        run, session, result = self._record()

        steps = self.store.get_steps(run.id)
        effect_steps = [s for s in steps if s.kind == "effect"]
        self.assertEqual(len(effect_steps), 2)
        self.assertEqual(session.effect_live, 2)
        self.assertEqual(session.effect_hits, 0)
        self.assertEqual(self.state.calls, 2)
        self.assertEqual([s.effect_name for s in effect_steps], [EFFECT_NAME, EFFECT_NAME])
        self.assertEqual(result["first"]["echo_path"], "/price?sku=SKU-1")
        self.assertEqual(result["second"]["echo_body"], '{"sku":"SKU-1","qty":2}')

    def test_replay_hits_cache_without_touching_upstream(self) -> None:
        run, _, recorded = self._record()
        calls_after_record = self.state.calls
        artifacts_after_record = list(FETCH_ARTIFACTS)

        _, session, replayed, error = self._replay(run.id)

        self.assertIsNone(error)
        self.assertEqual(session.effect_hits, 2)
        self.assertEqual(session.effect_live, 0)
        # 外部服务零新增请求
        self.assertEqual(self.state.calls, calls_after_record)
        # 响应体逐字节一致（含随机 nonce）
        self.assertEqual(replayed, recorded)
        # 回放期间 Agent 又跑了一次（因此列表新增一项），但其内容必须与录制时逐字节相同
        self.assertEqual(len(FETCH_ARTIFACTS), 2)
        self.assertEqual(FETCH_ARTIFACTS[-1], artifacts_after_record[0])

    def test_changed_request_body_does_not_falsely_hit(self) -> None:
        run, _, _ = self._record()

        _, session, _, error = self._replay(run.id, sku="SKU-2")

        self.assertIsInstance(error, Divergence)
        assert error is not None
        self.assertEqual(error.kind, "effect_miss")
        # 拒绝真实执行：外部服务未收到任何新请求
        self.assertEqual(session.effect_live, 0)
        self.assertEqual(self.state.calls, 2)

    def test_divergence_report_mentions_the_request_key(self) -> None:
        run, _, _ = self._record()
        _, _, _, error = self._replay(run.id, sku="SKU-9")
        assert error is not None
        self.assertIn("http", str(error))
        self.assertIn("body-sha256", str(error))


class TestPassthrough(InterceptTestCase):
    def test_passthrough_host_is_not_recorded(self) -> None:
        run = self.store.create_run("passthrough", mode="record")
        session = Recorder(self.store, run, client=_NullClient())

        async def agent(s: Any) -> str:
            host = httpx.URL(self.base_url).host
            async with instrument(s, passthrough_hosts=[host]) as http:
                resp = await http.get(f"{self.base_url}/health")
                return resp.text

        asyncio.run(agent(session))
        self.store.finish_run(run.id)

        # 直通的请求不进副作用通道
        self.assertEqual(session.effect_live, 0)
        self.assertEqual([s for s in self.store.get_steps(run.id) if s.kind == "effect"], [])
        # 但它确实真的发出去了
        self.assertEqual(self.state.calls, 1)


if __name__ == "__main__":
    unittest.main()
