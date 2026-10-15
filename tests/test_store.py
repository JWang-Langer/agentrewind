# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""存储层单元测试：运行/步骤/副作用/前缀解析/谱系/统计/离线分叉。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401
from agentrewind.branch import fork_run
from agentrewind.models import Effect, Step
from agentrewind.store import Store
from agentrewind.ulid import new_ulid, ulid_timestamp_ms


class TestUlid(unittest.TestCase):
    def test_length_and_charset(self) -> None:
        value = new_ulid()
        self.assertEqual(len(value), 26)
        self.assertTrue(all(ch in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for ch in value))

    def test_sorted_by_time(self) -> None:
        early = new_ulid(timestamp_ms=1_000)
        late = new_ulid(timestamp_ms=2_000)
        self.assertLess(early, late)
        self.assertEqual(ulid_timestamp_ms(early), 1_000)

    def test_rejects_bad_timestamp(self) -> None:
        with self.assertRaises(ValueError):
            new_ulid(timestamp_ms=-1)


class TestStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "arw.db")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_create_and_finish_run(self) -> None:
        run = self.store.create_run("demo", mode="record", meta={"k": "v"})
        self.assertEqual(run.status, "running")
        self.store.finish_run(run.id, "finished")
        fetched = self.store.get_run(run.id)
        assert fetched is not None
        self.assertEqual(fetched.status, "finished")
        self.assertEqual(fetched.meta, {"k": "v"})

    def test_append_and_read_steps_in_order(self) -> None:
        run = self.store.create_run("demo")
        for i in range(3):
            self.store.append_step(
                Step(run_id=run.id, seq=self.store.next_seq(run.id), kind="note", effect_value=i)
            )
        steps = self.store.get_steps(run.id)
        self.assertEqual([s.seq for s in steps], [0, 1, 2])
        self.assertEqual(self.store.next_seq(run.id), 3)

    def test_resolve_run_by_prefix_and_ambiguity(self) -> None:
        run = self.store.create_run("alpha")
        self.assertEqual(self.store.resolve_run(run.id[:10]).id, run.id)  # type: ignore[union-attr]
        self.assertIsNone(self.store.resolve_run("no-such-run"))

    def test_effects_lookup_across_runs(self) -> None:
        run = self.store.create_run("demo")
        self.store.append_effect(Effect(name="api", key="K1", value={"v": 1}), run.id, 0)
        found, value = self.store.lookup_effect("api", "K1")
        self.assertTrue(found)
        self.assertEqual(value, {"v": 1})
        found_missing, _ = self.store.lookup_effect("api", "K9")
        self.assertFalse(found_missing)

    def test_effect_value_round_trip_preserves_key_order(self) -> None:
        """回归测试：副作用值必须逐字节忠实往返（不排序键）。

        背景：早期实现用 sort_keys=True 落库，回读后键序变成字典序，
        导致放进 prompt 的 JSON 字节不同、回放指纹漂移——真实踩到过的缺陷。
        """
        run = self.store.create_run("demo")
        value = {"sku": "SKU-1", "price": 199.0, "currency": "CNY"}
        self.store.append_effect(Effect(name="api", key="K1", value=value), run.id, 0)

        found, restored = self.store.lookup_effect("api", "K1")

        self.assertTrue(found)
        self.assertEqual(list(restored.keys()), list(value.keys()))
        self.assertEqual(
            json.dumps(restored, ensure_ascii=False), json.dumps(value, ensure_ascii=False)
        )

    def test_find_responses_by_fingerprint(self) -> None:
        run = self.store.create_run("demo")
        self.store.append_step(
            Step(
                run_id=run.id,
                seq=0,
                kind="llm",
                fingerprint="fp1",
                request={"a": 1},
                response={"text": "x"},
            )
        )
        self.assertEqual(self.store.find_responses_by_fingerprint("fp1"), [(run.id, 0)])
        self.assertEqual(self.store.find_responses_by_fingerprint("nope"), [])

    def test_lineage_walks_parents(self) -> None:
        parent = self.store.create_run("p")
        child = self.store.create_run("c", mode="rewind", parent_run_id=parent.id, parent_seq=1)
        grand = self.store.create_run("g", mode="rewind", parent_run_id=child.id, parent_seq=0)
        self.assertEqual(
            [r.id for r in self.store.lineage(grand.id)], [parent.id, child.id, grand.id]
        )

    def test_fork_copies_prefix_only(self) -> None:
        run = self.store.create_run("p")
        for i in range(4):
            self.store.append_step(Step(run_id=run.id, seq=i, kind="note", effect_value=i))
        forked = fork_run(self.store, run.id, upto_seq=2)
        self.assertEqual([s.seq for s in self.store.get_steps(forked.id)], [0, 1])
        self.assertEqual(forked.parent_run_id, run.id)
        self.assertEqual(forked.mode, "rewind")
        self.assertEqual(len(self.store.get_steps(run.id)), 4)

    def test_stats(self) -> None:
        run = self.store.create_run("p")
        self.store.append_step(Step(run_id=run.id, seq=0, kind="llm", fingerprint="f"))
        self.store.append_effect(Effect(name="e", key="k", value=1), run.id, 1)
        stats = self.store.stats()
        self.assertEqual(stats["runs"], 1)
        self.assertEqual(stats["llm_steps"], 1)
        self.assertEqual(stats["effects"], 1)


if __name__ == "__main__":
    unittest.main()
