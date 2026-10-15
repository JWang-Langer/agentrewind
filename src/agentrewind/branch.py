# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""分支对齐与差异定位：回答"两条运行在哪一步分开、分开了什么、代价差多少"。"""

from __future__ import annotations

import difflib
from typing import Any

from agentrewind.models import Run, Step
from agentrewind.store import Store

SAME = "same"
DIVERGED = "diverged"
ONLY_LEFT = "only_left"
ONLY_RIGHT = "only_right"


def align_runs(left: list[Step], right: list[Step]) -> list[dict[str, Any]]:
    """按 seq 对齐两条运行的步骤。seq 是录制时的写入顺序，因此可直接比较。"""
    size = max(len(left), len(right))
    aligned: list[dict[str, Any]] = []
    for idx in range(size):
        a = left[idx] if idx < len(left) else None
        b = right[idx] if idx < len(right) else None
        if a is None:
            status = ONLY_RIGHT
        elif b is None:
            status = ONLY_LEFT
        elif a.kind != b.kind:
            status = DIVERGED
        elif a.kind == "llm":
            status = SAME if a.fingerprint == b.fingerprint else DIVERGED
        elif a.kind == "effect":
            same_effect = (a.effect_name, a.effect_key) == (b.effect_name, b.effect_key)
            same_value = a.effect_value == b.effect_value
            status = SAME if (same_effect and same_value) else DIVERGED
        elif a.kind == "note":
            # 注释步是 Agent 自己写的诊断信息，**不参与确定性判定**：
            # 它不影响任何请求指纹，若把它算作分歧点会掩盖真正的分叉位置。
            status = SAME
        else:
            status = SAME if a.effect_value == b.effect_value else DIVERGED
        item = {"seq": idx, "status": status, "left": a, "right": b}
        if a is not None and b is not None and a.kind == "note":
            item["note_differs"] = a.effect_value != b.effect_value
        aligned.append(item)
    return aligned


def first_divergence(aligned: list[dict[str, Any]]) -> int | None:
    for item in aligned:
        if item["status"] != SAME:
            return int(item["seq"])
    return None


def _token_total(steps: list[Step]) -> int:
    total = 0
    for step in steps:
        if step.kind == "llm" and step.response:
            total += int(step.response.get("prompt_tokens") or 0)
            total += int(step.response.get("completion_tokens") or 0)
    return total


def _latency_total(steps: list[Step]) -> float:
    return round(sum(step.duration_ms or 0.0 for step in steps), 3)


def _tool_names(step: Step | None) -> list[str]:
    """从步骤的响应里取出工具调用名（Step 本身没有 tool_calls，响应里才有）。"""
    if step is None or not step.response:
        return []
    return [str(call.get("name", "")) for call in (step.response.get("tool_calls") or [])]


def _text_delta(a: Step, b: Step) -> list[str]:
    if not (a.response and b.response):
        return []
    left = (a.response.get("text") or "").splitlines()
    right = (b.response.get("text") or "").splitlines()
    return list(difflib.unified_diff(left, right, lineterm="", n=1))


def diff_runs(store: Store, left_id: str, right_id: str, limit: int = 20) -> dict[str, Any]:
    """完整的差异报告：分叉点、逐步差异、工具调用差异、token/耗时差。"""
    left_steps = store.get_steps(left_id)
    right_steps = store.get_steps(right_id)
    aligned = align_runs(left_steps, right_steps)
    fork_at = first_divergence(aligned)

    details: list[dict[str, Any]] = []
    for item in aligned:
        if item["status"] == SAME:
            continue
        a, b = item["left"], item["right"]
        detail: dict[str, Any] = {
            "seq": item["seq"],
            "status": item["status"],
            "kind": (a.kind if a else (b.kind if b else None)),
            "left_fingerprint": getattr(a, "fingerprint", None),
            "right_fingerprint": getattr(b, "fingerprint", None),
            "left_tool_calls": _tool_names(a),
            "right_tool_calls": _tool_names(b),
            "text_delta": _text_delta(a, b) if (a and b) else [],
        }
        details.append(detail)
        if len(details) >= limit:
            break

    left_tokens, right_tokens = _token_total(left_steps), _token_total(right_steps)
    return {
        "left_run": left_id,
        "right_run": right_id,
        "left_steps": len(left_steps),
        "right_steps": len(right_steps),
        "fork_at_seq": fork_at,
        "diverged_steps": sum(1 for i in aligned if i["status"] != SAME),
        "note_differences": sum(1 for i in aligned if i.get("note_differs")),
        "details": details,
        "left_tokens": left_tokens,
        "right_tokens": right_tokens,
        "token_delta": right_tokens - left_tokens,
        "left_latency_ms": _latency_total(left_steps),
        "right_latency_ms": _latency_total(right_steps),
    }


def fork_run(
    store: Store,
    run_id: str,
    upto_seq: int,
    name: str | None = None,
) -> Run:
    """离线分叉：把某运行的前 upto_seq 步复制成一个新运行（不重跑、不调用上游）。

    用途：给"如果当初在第 N 步换成另一种做法"留一个可对比的基线；
    与 Replayer(on_divergence='live') 配合可实现"改一行、从第 N 步起真跑"。
    """
    source = store.get_run(run_id)
    if source is None:
        raise ValueError(f"run not found: {run_id}")
    steps = [s for s in store.get_steps(run_id) if s.seq < upto_seq]
    new_run = store.create_run(
        name=name or f"{source.name}#fork@{upto_seq}",
        mode="rewind",
        meta={"forked_from": run_id, "upto_seq": upto_seq},
        parent_run_id=run_id,
        parent_seq=upto_seq,
    )
    for step in steps:
        copied = step.model_copy(update={"run_id": new_run.id})
        store.append_step(copied)
    store.finish_run(new_run.id)
    return new_run
