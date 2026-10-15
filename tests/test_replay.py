# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""端到端测试：真实 HTTP stub 上游 + 示例 Agent，验证录制 / 回放 / 分歧 / 回退分叉 / 差异。

这些测试是项目的核心契约：
1. 回放必须**逐位一致**（结果字符串里的 nonce 与录制时完全相同）；
2. 回放**不得**产生任何上游调用（stub 的调用计数不变）；
3. 代码或输入改变时，**首个不一致的步**被精确定位，并给出请求级差异路径；
4. 副作用在回放时**不重复执行**（producer 计数不变）。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import Any

from demo_agent import DEFAULT_SYSTEM_PROMPT, TOOLS
from demo_agent import main as demo_main

import tests  # noqa: F401
from agentrewind.branch import diff_runs
from agentrewind.cli import main as cli_main
from agentrewind.clients import OpenAICompatClient
from agentrewind.models import LLMRequest
from agentrewind.recorder import Divergence, Recorder, Replayer
from agentrewind.store import Store
from agentrewind.testing.stub_server import start_stub

PRODUCER_CALLS: list[str] = []


async def counter_agent(session: Any) -> str:
    """带计数副作用的 Agent：用于证明回放不会重复执行副作用。"""
    first = await session.complete(
        LLMRequest(
            model="stub-model",
            messages=[
                {"role": "system", "content": "必须 LOOKUP"},
                {"role": "user", "content": "SKU-1 价格"},
            ],
            temperature=0.0,
        )
    )
    if first.tool_calls:

        def producer() -> dict[str, Any]:
            PRODUCER_CALLS.append("called")
            return {"sku": "SKU-1", "price": 199.0}

        await session.effect("price_api", "SKU-1", producer)
    return first.text or "no-text"


async def llm_only_agent(session: Any) -> str:
    """只发一次 LLM 调用、**不做副作用**：用于验证 effect_miss 被拒绝。"""
    response = await session.complete(
        LLMRequest(
            model="stub-model",
            messages=[
                {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
                {"role": "user", "content": "帮我看下 SKU-1 现在多少钱"},
            ],
            tools=TOOLS,
            temperature=0.0,
        )
    )
    return response.text or ""


async def one_call_agent(session: Any) -> str:
    """发一次 LLM 调用 + 做一次副作用（前半段与 demo_main 完全一致）。

    用于构造 extra_call 场景：录制停在这里，回放却要继续走到第二次 LLM 调用。
    """
    response = await session.complete(
        LLMRequest(
            model="stub-model",
            messages=[
                {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
                {"role": "user", "content": "帮我看下 SKU-1 现在多少钱"},
            ],
            tools=TOOLS,
            temperature=0.0,
        )
    )
    call = response.tool_calls[0]
    await session.effect(
        "price_api",
        "SKU-1",
        lambda: {"sku": "SKU-1", "price": 199.0, "currency": "CNY"},
    )
    return call.name


async def three_call_agent(session: Any) -> str:
    """连续三次一样的调用：证明"同输入不同次数"也会被抓住。"""
    last = ""
    for _ in range(3):
        response = await session.complete(
            LLMRequest(
                model="stub-model",
                messages=[{"role": "user", "content": "SKU-1 价格"}],
                temperature=0.0,
            )
        )
        last = response.text or last
    return last


class ReplayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "arw.db")
        self.server, self.state, self.base_url = start_stub()
        PRODUCER_CALLS.clear()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------------ helpers
    def _client(self) -> OpenAICompatClient:
        return OpenAICompatClient(base_url=self.base_url, api_key="test-key")

    def _record(self, agent: Any, **mutations: Any) -> tuple[Any, Any, Any]:
        run = self.store.create_run("record", mode="record", meta=dict(mutations))
        client = self._client()
        session = Recorder(self.store, run, client=client, mutations=mutations)

        async def go() -> Any:
            try:
                return await agent(session)
            finally:
                await client.aclose()

        result = asyncio.run(go())
        self.store.finish_run(run.id)
        return run, session, result

    def _replay(
        self,
        source_run_id: str,
        agent: Any,
        on_divergence: str = "raise",
        mutations: dict[str, Any] | None = None,
    ) -> tuple[Any, Any, Any, Exception | None]:
        run = self.store.create_run(
            "replay", mode="replay", parent_run_id=source_run_id, parent_seq=0
        )
        client = self._client() if on_divergence == "live" else None
        session = Replayer(
            self.store,
            run,
            source_run_id=source_run_id,
            client=client,
            on_divergence=on_divergence,
            mutations=mutations or {},
        )

        async def go() -> Any:
            try:
                return await agent(session)
            finally:
                if client is not None:
                    await client.aclose()

        error: Exception | None = None
        try:
            result = asyncio.run(go())
        except Divergence as exc:
            result = None
            error = exc
        self.store.finish_run(run.id, "diverged" if error else "finished")
        return run, session, result, error


class TestRecordAndReplayDeterminism(ReplayTestCase):
    def test_record_shape(self) -> None:
        run, session, result = self._record(demo_main)
        steps = self.store.get_steps(run.id)
        self.assertEqual([s.kind for s in steps[:2]], ["note", "llm"])
        self.assertEqual(session.live_calls, 2)
        self.assertEqual(session.effect_live, 1)
        self.assertIn("SKU-1", result)
        self.assertEqual(self.state.calls, 2)

    def test_replay_is_bit_exact_and_makes_no_upstream_call(self) -> None:
        run, _, recorded = self._record(demo_main)
        calls_after_record = self.state.calls

        replay_run, session, replayed, error = self._replay(run.id, demo_main)

        self.assertIsNone(error)
        self.assertIsNone(session.divergence)
        self.assertEqual(session.hits, 2)
        self.assertEqual(session.expected_llm_steps, 2)
        self.assertEqual(session.live_calls, 0)
        # 位级一致：nonce 完全来自录制响应
        self.assertEqual(replayed, recorded)
        # 零上游调用：回放没有碰网络
        self.assertEqual(self.state.calls, calls_after_record)
        self.assertEqual(len(self.store.get_steps(replay_run.id)), 4)

    def test_effect_producer_is_not_executed_on_replay(self) -> None:
        run, _, _ = self._record(counter_agent)
        executed_during_record = len(PRODUCER_CALLS)
        self.assertEqual(executed_during_record, 1)

        _, session, _, error = self._replay(run.id, counter_agent)

        self.assertIsNone(error)
        self.assertEqual(session.effect_hits, 1)
        self.assertEqual(session.effect_live, 0)
        # 回放没有再次执行副作用
        self.assertEqual(len(PRODUCER_CALLS), executed_during_record)


class TestDivergenceDetection(ReplayTestCase):
    def test_prompt_change_detected_at_first_call_with_request_level_diff(self) -> None:
        run, _, _ = self._record(demo_main)

        _, session, _, error = self._replay(
            run.id, demo_main, mutations={"system_prompt": "你是一个只说 yes 的助手。"}
        )

        self.assertIsInstance(error, Divergence)
        assert error is not None
        self.assertEqual(error.kind, "fingerprint_mismatch")
        # 步骤序列：seq0=note, seq1=第 1 次 LLM 调用
        self.assertEqual(error.seq, 1)
        paths = {item["path"] for item in error.diffs}
        self.assertIn("messages[0].content", paths)
        self.assertIsNotNone(session.divergence)

    def test_extra_call_is_detected(self) -> None:
        # 录制只有 1 次 LLM 调用，而 demo_main 需要 2 次
        run, _, _ = self._record(one_call_agent)
        _, session, _, error = self._replay(run.id, demo_main)
        self.assertIsInstance(error, Divergence)
        assert error is not None
        self.assertEqual(error.kind, "extra_call")
        self.assertEqual(session.hits, 1)
        self.assertEqual(session.expected_llm_steps, 1)

    def test_effect_miss_is_refused_instead_of_executed(self) -> None:
        # 录制里没有 price_api 的副作用；回放必须拒绝真实执行以保证确定性
        run, _, _ = self._record(llm_only_agent)
        _, session, _, error = self._replay(run.id, demo_main)
        self.assertIsInstance(error, Divergence)
        assert error is not None
        self.assertEqual(error.kind, "effect_miss")
        self.assertEqual(session.effect_live, 0)
        self.assertEqual(session.hits, 1)

    def test_different_input_is_reported_as_fingerprint_mismatch(self) -> None:
        run, _, _ = self._record(demo_main)
        _, _, _, error = self._replay(run.id, three_call_agent)
        self.assertIsInstance(error, Divergence)
        assert error is not None
        self.assertEqual(error.kind, "fingerprint_mismatch")


class TestRewindAndDiff(ReplayTestCase):
    def test_rewind_switches_to_live_at_divergence_point(self) -> None:
        run, _, recorded = self._record(demo_main)
        calls_after_record = self.state.calls

        rewind_run, session, result, error = self._replay(
            run.id,
            demo_main,
            on_divergence="live",
            mutations={"second_note": " 备注：这是回退后的新分支"},
        )

        self.assertIsNone(error)
        self.assertIsNotNone(session.divergence)
        assert session.divergence is not None
        # 步骤序列：seq0=note, seq1=llm1, seq2=effect, seq3=llm2；改动只影响第二次调用
        self.assertEqual(session.divergence.seq, 3)
        self.assertEqual(session.divergence.kind, "fingerprint_mismatch")
        self.assertEqual(session.hits, 1)
        self.assertEqual(session.live_calls, 1)
        self.assertEqual(session.effect_hits, 1)
        self.assertEqual(self.state.calls, calls_after_record + 1)
        self.assertIsNotNone(result)
        self.assertEqual(self.store.get_run(rewind_run.id).parent_run_id, run.id)  # type: ignore[union-attr]

    def test_diff_locates_fork_point(self) -> None:
        run, _, _ = self._record(demo_main)
        rewind_run, _, _, _ = self._replay(
            run.id, demo_main, on_divergence="live", mutations={"second_note": " 新分支"}
        )

        report = diff_runs(self.store, run.id, rewind_run.id)

        self.assertEqual(report["fork_at_seq"], 3)
        self.assertGreaterEqual(report["diverged_steps"], 1)
        self.assertEqual(report["details"][0]["seq"], 3)
        self.assertEqual(report["details"][0]["kind"], "llm")
        # 注释步（seq0）内容是变化的，但它不算分歧点
        self.assertGreaterEqual(report["note_differences"], 1)

    def test_cli_verify_exit_codes(self) -> None:
        run, _, _ = self._record(demo_main)
        store_path = str(self.store.path)

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            ok_code = cli_main(
                ["--store", store_path, "--json", "verify", run.id, "--agent", "demo_agent:main"]
            )
        self.assertEqual(ok_code, 0)

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            diverged_code = cli_main(
                [
                    "--store",
                    store_path,
                    "--json",
                    "verify",
                    run.id,
                    "--agent",
                    "demo_agent:main",
                    "--set",
                    "system_prompt=完全不同的提示词",
                ]
            )
        self.assertEqual(diverged_code, 3)


if __name__ == "__main__":
    unittest.main()
