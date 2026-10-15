# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""请求指纹：把一次 LLM 请求压成一个稳定哈希，并解释"为什么两次请求不同"。

设计要点（技术独创性的一部分，必须写清楚）：
1. 规范化序列化：字典按键排序、无多余空白、UTF-8 不转义，保证 dict 插入顺序不影响结果。
2. 语义显式：None 与 0 语义不同（temperature=None 表示"用服务端默认"），因此**不丢弃 None**。
3. 版本前缀：规范化规则一旦改变，旧指纹必须失效，所以把 SCHEMA_VERSION 混入哈希。
4. 可解释：只给哈希值无法调试，因此提供 fingerprint_diff 指出具体差异路径。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agentrewind.models import LLMRequest

SCHEMA_VERSION = "arw-fp-1"

_ABSENT = object()


def canonical_json(value: Any) -> str:
    """确定性 JSON：键排序、紧凑分隔符、保留 Unicode。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_request(req: LLMRequest) -> dict[str, Any]:
    return req.model_dump(mode="json", exclude_none=False)


def request_fingerprint(req: LLMRequest) -> str:
    """返回 32 位十六进制指纹（128 bit）。"""
    payload = {"v": SCHEMA_VERSION, "req": _canonical_request(req)}
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return digest[:32]


def _walk(a: Any, b: Any, path: str, out: list[dict[str, Any]]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            child = f"{path}.{key}" if path else str(key)
            va = a.get(key, _ABSENT)
            vb = b.get(key, _ABSENT)
            if va is _ABSENT or vb is _ABSENT:
                out.append(
                    {
                        "path": child,
                        "kind": "added" if va is _ABSENT else "removed",
                        "left": None if va is _ABSENT else va,
                        "right": None if vb is _ABSENT else vb,
                    }
                )
            else:
                _walk(va, vb, child, out)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(
                {"path": f"{path}.length", "kind": "changed", "left": len(a), "right": len(b)}
            )
        for idx, (va, vb) in enumerate(zip(a, b, strict=False)):
            _walk(va, vb, f"{path}[{idx}]", out)
        return
    if a != b:
        out.append({"path": path, "kind": "changed", "left": a, "right": b})


def fingerprint_diff(left: LLMRequest, right: LLMRequest) -> list[dict[str, Any]]:
    """列出两次请求的差异路径。返回空列表表示指纹必然相同。"""
    diffs: list[dict[str, Any]] = []
    _walk(_canonical_request(left), _canonical_request(right), "", diffs)
    return diffs


def same_request(left: LLMRequest, right: LLMRequest) -> bool:
    return request_fingerprint(left) == request_fingerprint(right)
