# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""录制器与回放器：Agent 只与 Session 交互，因此与任何 Agent 框架无关。

- Recorder：真实调用上游，按指纹落库。
- Replayer：按源运行的步骤序列逐条比对指纹；命中则返回**录制时的原始响应**，
  首次不一致即判定为分歧点（这正好就是"回退到某一步改一改"的分叉位置），
  可选择 live 模式从分歧点开始真实调用并继续记录。
- 副作用经 session.effect() 走同一条确定性通道（命中则**不执行**真实副作用）。
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from agentrewind.clients import LLMClient
from agentrewind.fingerprint import fingerprint_diff, request_fingerprint
from agentrewind.models import Effect, LLMRequest, LLMResponse, Run, Step
from agentrewind.store import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Divergence(RuntimeError):
    """回放与录制不一致。这是有价值的信息，不是异常噪声：它就是分叉点。"""

    def __init__(
        self,
        *,
        seq: int,
        kind: str,
        message: str,
        expected_fingerprint: str | None = None,
        actual_fingerprint: str | None = None,
        diffs: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.seq = seq
        self.kind = kind
        self.expected_fingerprint = expected_fingerprint
        self.actual_fingerprint = actual_fingerprint
        self.diffs = diffs or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "message": str(self),
            "expected_fingerprint": self.expected_fingerprint,
            "actual_fingerprint": self.actual_fingerprint,
            "diffs": self.diffs,
        }


class Session:
    """Agent 唯一被允许使用的 LLM / 副作用入口。"""

    def __init__(
        self,
        store: Store,
        run: Run,
        mode: str,
        *,
        client: LLMClient | None = None,
        mutations: dict[str, Any] | None = None,
        on_divergence: str = "raise",
    ) -> None:
        if mode not in ("record", "replay", "rewind"):
            raise ValueError(f"unknown mode: {mode}")
        if on_divergence not in ("raise", "live"):
            raise ValueError(f"unknown on_divergence policy: {on_divergence}")
        self.store = store
        self.run = run
        self.mode = mode
        self.client = client
        self.on_divergence = on_divergence
        self.seq = store.next_seq(run.id)
        self.hits = 0
        self.live_calls = 0
        self.extra_calls = 0
        self.effect_hits = 0
        self.effect_live = 0
        self.divergence: Divergence | None = None
        self._mutations = dict(mutations or {})

    # ------------------------------------------------------------------ helpers
    def mutation(self, name: str, default: Any = None) -> Any:
        """读取本次回退注入的改动（例如改 system prompt / 改工具集）。"""
        return self._mutations.get(name, default)

    @property
    def mutations(self) -> dict[str, Any]:
        return dict(self._mutations)

    def note(self, text: str) -> Step:
        return self._append(kind="note", effect_name=None, effect_value=text)

    def _append(self, **kwargs: Any) -> Step:
        step = Step(
            run_id=self.run.id,
            seq=self.seq,
            created_at=_now(),
            **kwargs,
        )
        self.store.append_step(step)
        self.seq += 1
        return step

    # ------------------------------------------------------------------ the API
    async def complete(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover - interface
        raise NotImplementedError

    async def effect(
        self,
        name: str,
        key: str,
        producer: Callable[[], Any] | Callable[[], Awaitable[Any]],
    ) -> Any:
        raise NotImplementedError

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run.id,
            "run_name": self.run.name,
            "mode": self.mode,
            "steps": self.seq,
            "llm_hits": self.hits,
            "llm_live_calls": self.live_calls,
            "llm_extra_calls": self.extra_calls,
            "effect_hits": self.effect_hits,
            "effect_live": self.effect_live,
            "divergence": None if self.divergence is None else self.divergence.to_dict(),
        }


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class Recorder(Session):
    """真实执行并录制。"""

    def __init__(self, store: Store, run: Run, client: LLMClient, **kwargs: Any) -> None:
        if client is None:
            raise ValueError("Recorder requires a live client")
        super().__init__(store, run, "record", client=client, **kwargs)
        self._live: LLMClient = client

    async def complete(self, req: LLMRequest) -> LLMResponse:
        fingerprint = request_fingerprint(req)
        started = time.perf_counter()
        try:
            response = await self._live.complete(req)
        except Exception as exc:  # noqa: BLE001 - 失败也要落库，便于回放"复现失败"
            self._append(
                kind="llm",
                fingerprint=fingerprint,
                request=req.model_dump(mode="json"),
                response=None,
                duration_ms=(time.perf_counter() - started) * 1000,
                status="error",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        duration = (time.perf_counter() - started) * 1000
        self.live_calls += 1
        self._append(
            kind="llm",
            fingerprint=fingerprint,
            request=req.model_dump(mode="json"),
            response=response.model_dump(mode="json"),
            duration_ms=duration,
        )
        return response

    async def effect(
        self,
        name: str,
        key: str,
        producer: Callable[[], Any] | Callable[[], Awaitable[Any]],
    ) -> Any:
        value = await _maybe_await(producer())
        self.effect_live += 1
        self.store.append_effect(Effect(name=name, key=key, value=value), self.run.id, self.seq)
        self._append(kind="effect", effect_name=name, effect_key=key, effect_value=value)
        return value


class Replayer(Session):
    """按源运行的步骤序列确定性回放；首次指纹不一致即分叉点。"""

    def __init__(
        self,
        store: Store,
        run: Run,
        source_run_id: str,
        client: LLMClient | None = None,
        on_divergence: str = "raise",
        **kwargs: Any,
    ) -> None:
        mode = "rewind" if on_divergence == "live" else "replay"
        super().__init__(store, run, mode, client=client, on_divergence=on_divergence, **kwargs)
        self.source_run_id = source_run_id
        self._expected = [s for s in store.get_steps(source_run_id) if s.kind == "llm"]
        self._idx = 0
        self._diverged = False

    @property
    def expected_llm_steps(self) -> int:
        return len(self._expected)

    def _raise_or_live(self, exc: Divergence, req: LLMRequest | None) -> None:
        self.divergence = exc
        if self.on_divergence == "raise":
            raise exc
        self._diverged = True

    async def complete(self, req: LLMRequest) -> LLMResponse:
        fingerprint = request_fingerprint(req)

        if not self._diverged:
            if self._idx >= len(self._expected):
                exc = Divergence(
                    seq=self.seq,
                    kind="extra_call",
                    message=(
                        f"回放到此结束：录制的 {len(self._expected)} 次 LLM 调用已用完，"
                        f"但 Agent 又发起了第 {self._idx + 1} 次调用"
                    ),
                )
                self._raise_or_live(exc, req)
            else:
                expected = self._expected[self._idx]
                if expected.fingerprint != fingerprint:
                    recorded_req = LLMRequest(**expected.request) if expected.request else None
                    diffs = fingerprint_diff(recorded_req, req) if recorded_req else []
                    exc = Divergence(
                        seq=self.seq,
                        kind="fingerprint_mismatch",
                        message=(
                            f"第 {self.seq} 步（第 {self._idx + 1} 次 LLM 调用）与录制不一致，"
                            f"共 {len(diffs)} 处差异，这是本次分叉点"
                        ),
                        expected_fingerprint=expected.fingerprint,
                        actual_fingerprint=fingerprint,
                        diffs=diffs,
                    )
                    self._raise_or_live(exc, req)
                else:
                    response = LLMResponse(**expected.response)  # type: ignore[arg-type]
                    self.hits += 1
                    self._idx += 1
                    self._append(
                        kind="llm",
                        fingerprint=fingerprint,
                        request=req.model_dump(mode="json"),
                        response=response.model_dump(mode="json"),
                        duration_ms=expected.duration_ms,
                    )
                    return response

        if self.client is None:
            raise RuntimeError("回放已进入 live 模式，但未提供 client")
        started = time.perf_counter()
        response = await self.client.complete(req)
        duration = (time.perf_counter() - started) * 1000
        self.live_calls += 1
        self._append(
            kind="llm",
            fingerprint=fingerprint,
            request=req.model_dump(mode="json"),
            response=response.model_dump(mode="json"),
            duration_ms=duration,
        )
        return response

    async def effect(
        self,
        name: str,
        key: str,
        producer: Callable[[], Any] | Callable[[], Awaitable[Any]],
    ) -> Any:
        found, value = self.store.lookup_effect(name, key)
        if found:
            self.effect_hits += 1
            self.store.append_effect(Effect(name=name, key=key, value=value), self.run.id, self.seq)
            self._append(kind="effect", effect_name=name, effect_key=key, effect_value=value)
            return value

        if self.on_divergence == "raise" and not self._diverged:
            exc = Divergence(
                seq=self.seq,
                kind="effect_miss",
                message=(
                    f"副作用 '{name}' (key={key!r}) 在录制中不存在；"
                    f"回放模式下拒绝真实执行以保证确定性"
                ),
            )
            self._raise_or_live(exc, None)

        value = await _maybe_await(producer())
        self.effect_live += 1
        self.store.append_effect(Effect(name=name, key=key, value=value), self.run.id, self.seq)
        self._append(kind="effect", effect_name=name, effect_key=key, effect_value=value)
        return value
