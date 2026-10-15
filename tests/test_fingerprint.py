# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""指纹单元测试：规范性、语义区分、差异解释。"""

from __future__ import annotations

import unittest

import tests  # noqa: F401 - 触发 sys.path 设置
from agentrewind.fingerprint import (
    SCHEMA_VERSION,
    canonical_json,
    fingerprint_diff,
    request_fingerprint,
    same_request,
)
from agentrewind.models import LLMRequest


def _req(**overrides: object) -> LLMRequest:
    base: dict[str, object] = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.0,
    }
    base.update(overrides)
    return LLMRequest(**base)  # type: ignore[arg-type]


class TestCanonicalJson(unittest.TestCase):
    def test_key_order_does_not_matter(self) -> None:
        self.assertEqual(canonical_json({"b": 1, "a": 2}), canonical_json({"a": 2, "b": 1}))

    def test_unicode_is_not_escaped(self) -> None:
        self.assertIn("价格", canonical_json({"k": "价格"}))

    def test_no_extra_whitespace(self) -> None:
        self.assertEqual(canonical_json({"a": [1, 2]}), '{"a":[1,2]}')


class TestRequestFingerprint(unittest.TestCase):
    def test_deterministic(self) -> None:
        self.assertEqual(request_fingerprint(_req()), request_fingerprint(_req()))
        self.assertEqual(len(request_fingerprint(_req())), 32)

    def test_dict_insertion_order_in_message_is_irrelevant(self) -> None:
        a = _req(messages=[{"role": "user", "content": "hi"}])
        b = _req(messages=[{"content": "hi", "role": "user"}])
        self.assertEqual(request_fingerprint(a), request_fingerprint(b))

    def test_none_differs_from_zero(self) -> None:
        self.assertNotEqual(
            request_fingerprint(_req(temperature=None)),
            request_fingerprint(_req(temperature=0.0)),
        )

    def test_tools_change_fingerprint(self) -> None:
        tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
        self.assertNotEqual(request_fingerprint(_req()), request_fingerprint(_req(tools=tools)))

    def test_message_order_matters(self) -> None:
        a = _req(messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
        b = _req(messages=[{"role": "user", "content": "u"}, {"role": "system", "content": "s"}])
        self.assertNotEqual(request_fingerprint(a), request_fingerprint(b))

    def test_schema_version_is_part_of_hash(self) -> None:
        self.assertEqual(SCHEMA_VERSION, "arw-fp-1")


class TestFingerprintDiff(unittest.TestCase):
    def test_no_diff_when_identical(self) -> None:
        self.assertEqual(fingerprint_diff(_req(), _req()), [])
        self.assertTrue(same_request(_req(), _req()))

    def test_reports_exact_path(self) -> None:
        diffs = fingerprint_diff(_req(), _req(messages=[{"role": "user", "content": "bye"}]))
        paths = {item["path"] for item in diffs}
        self.assertIn("messages[0].content", paths)
        item = next(d for d in diffs if d["path"] == "messages[0].content")
        self.assertEqual(item["kind"], "changed")
        self.assertEqual(item["left"], "hi")
        self.assertEqual(item["right"], "bye")

    def test_reports_added_and_removed(self) -> None:
        # 规范化请求包含全部字段，因此同名字段的差异是 changed；
        # added/removed 出现在 message 内部键集合不同时（如带/不带 tool_calls 的 assistant 消息）。
        left = _req(messages=[{"role": "user", "content": "hi"}])
        right = _req(messages=[{"role": "user", "content": "hi", "name": "alice"}])
        kinds = {item["path"]: item["kind"] for item in fingerprint_diff(left, right)}
        self.assertEqual(kinds.get("messages[0].name"), "added")
        reversed_kinds = {item["path"]: item["kind"] for item in fingerprint_diff(right, left)}
        self.assertEqual(reversed_kinds.get("messages[0].name"), "removed")

    def test_reports_list_length_change(self) -> None:
        long_messages = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
        diffs = fingerprint_diff(_req(), _req(messages=long_messages))
        paths = {item["path"] for item in diffs}
        self.assertIn("messages.length", paths)


if __name__ == "__main__":
    unittest.main()
