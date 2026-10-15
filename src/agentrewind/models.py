# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""数据模型：一次运行 = 一串有序的 Step（LLM 调用 / 副作用 / 注释）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

StepKind = Literal["llm", "effect", "note"]
RunMode = Literal["record", "replay", "rewind"]


class ToolCall(BaseModel):
    """模型请求的一次工具调用。arguments 保留原始 JSON 字符串，避免二次序列化引入差异。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: str = "{}"


class LLMRequest(BaseModel):
    """一次 LLM 请求的完整可比较形态（指纹的唯一输入）。"""

    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    stop: list[str] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """转成 OpenAI 兼容 /chat/completions 的请求体。"""
        payload: dict[str, Any] = {"model": self.model, "messages": self.messages}
        for key in ("tools", "tool_choice", "temperature", "top_p", "max_tokens", "seed", "stop"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        payload.update(self.extra)
        return payload


class LLMResponse(BaseModel):
    """一次 LLM 响应的规范化形态。raw 保留上游原文以便逐位回放。"""

    model_config = ConfigDict(extra="forbid")

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)


class Step(BaseModel):
    """运行中的一步。llm 步记录请求+响应；effect 步记录副作用的幂等键与结果。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    seq: int
    kind: StepKind
    fingerprint: str | None = None
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    effect_name: str | None = None
    effect_key: str | None = None
    effect_value: Any | None = None
    duration_ms: float | None = None
    status: Literal["ok", "error"] = "ok"
    error: str | None = None
    created_at: str | None = None


class Effect(BaseModel):
    """副作用（文件写、HTTP 调用、DB 写…）的录制单元：同名 + 同 key 视为同一次。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    key: str
    value: Any | None = None


class Run(BaseModel):
    """一次完整运行。parent_* 构成分支谱系，用于 rewind 后的溯源。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    mode: RunMode
    created_at: str
    meta: dict[str, Any] = Field(default_factory=dict)
    parent_run_id: str | None = None
    parent_seq: int | None = None
    status: str = "finished"
