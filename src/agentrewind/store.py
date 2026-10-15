# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""SQLite 事件日志（run / step / effect）。

为什么用 SQLite 而不是 JSONL：需要按指纹索引做跨运行命中查询，并且要支持
"复制前 N 步生成分支" 这类事务性操作。单文件、零运维，符合本地优先原则。

说明：存储层刻意保持**同步**实现（sqlite3 无异步驱动，asyncio.to_thread 只会
把复杂度转移给调用方）；网络与 Agent 逻辑是异步的，二者边界清晰。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentrewind.models import Effect, Run, Step
from agentrewind.ulid import new_ulid

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    mode          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running',
    meta          TEXT NOT NULL DEFAULT '{}',
    parent_run_id TEXT,
    parent_seq    INTEGER
);
CREATE TABLE IF NOT EXISTS steps (
    run_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    fingerprint  TEXT,
    request      TEXT,
    response     TEXT,
    effect_name  TEXT,
    effect_key   TEXT,
    effect_value TEXT,
    duration_ms  REAL,
    status       TEXT NOT NULL DEFAULT 'ok',
    error        TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_steps_fingerprint ON steps (fingerprint);
CREATE TABLE IF NOT EXISTS effects (
    run_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    name       TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_effects_name_key ON effects (name, key);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _dumps(value: Any) -> str:
    """存储序列化。

    刻意**不排序键**：payload 必须逐字节忠实往返，否则字典插入顺序会在
    存储→回读之间被改写，进而改变 prompt 字节、破坏回放的确定性
    （这是实测踩到的真实缺陷，见 docs/design-notes.md 的"隐式非确定性"一节）。
    """
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


class Store:
    """事件日志存储。所有写操作即时提交（Agent 崩溃时不能丢已发生的步骤）。"""

    def __init__(self, path: str | Path = ".agentrewind/arw.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -------------------------------------------------------------------- runs
    def create_run(
        self,
        name: str,
        mode: str = "record",
        meta: dict[str, Any] | None = None,
        parent_run_id: str | None = None,
        parent_seq: int | None = None,
        run_id: str | None = None,
    ) -> Run:
        run = Run(
            id=run_id or new_ulid(),
            name=name,
            mode=mode,  # type: ignore[arg-type]
            created_at=utcnow_iso(),
            meta=meta or {},
            parent_run_id=parent_run_id,
            parent_seq=parent_seq,
            status="running",
        )
        self._conn.execute(
            "INSERT INTO runs (id, name, mode, created_at, status, meta, parent_run_id, parent_seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.name,
                run.mode,
                run.created_at,
                run.status,
                _dumps(run.meta),
                run.parent_run_id,
                run.parent_seq,
            ),
        )
        self._conn.commit()
        return run

    def finish_run(self, run_id: str, status: str = "finished") -> None:
        self._conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))
        self._conn.commit()

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return Run(
            id=row["id"],
            name=row["name"],
            mode=row["mode"],
            created_at=row["created_at"],
            status=row["status"],
            meta=_loads(row["meta"]) or {},
            parent_run_id=row["parent_run_id"],
            parent_seq=row["parent_seq"],
        )

    def resolve_run(self, prefix: str) -> Run | None:
        """允许用 id/name 前缀定位运行（CLI 友好），前缀歧义时抛 ValueError。"""
        if self.get_run(prefix) is not None:
            return self.get_run(prefix)
        rows = self._conn.execute(
            "SELECT id FROM runs WHERE id LIKE ? OR name LIKE ? ORDER BY created_at DESC",
            (f"{prefix}%", f"{prefix}%"),
        ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise ValueError(f"run prefix '{prefix}' is ambiguous ({len(rows)} matches)")
        return self.get_run(rows[0]["id"])

    def list_runs(self, limit: int = 50) -> list[Run]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self.get_run(row["id"]) for row in rows]  # type: ignore[misc]

    # ------------------------------------------------------------------- steps
    def append_step(self, step: Step) -> Step:
        created = step.created_at or utcnow_iso()
        self._conn.execute(
            "INSERT INTO steps (run_id, seq, kind, fingerprint, request, response, effect_name,"
            " effect_key, effect_value, duration_ms, status, error, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                step.run_id,
                step.seq,
                step.kind,
                step.fingerprint,
                None if step.request is None else _dumps(step.request),
                None if step.response is None else _dumps(step.response),
                step.effect_name,
                step.effect_key,
                None if step.effect_value is None else _dumps(step.effect_value),
                step.duration_ms,
                step.status,
                step.error,
                created,
            ),
        )
        self._conn.commit()
        step.created_at = created
        return step

    def _row_to_step(self, row: sqlite3.Row) -> Step:
        return Step(
            run_id=row["run_id"],
            seq=row["seq"],
            kind=row["kind"],
            fingerprint=row["fingerprint"],
            request=_loads(row["request"]),
            response=_loads(row["response"]),
            effect_name=row["effect_name"],
            effect_key=row["effect_key"],
            effect_value=_loads(row["effect_value"]),
            duration_ms=row["duration_ms"],
            status=row["status"],
            error=row["error"],
            created_at=row["created_at"],
        )

    def get_steps(self, run_id: str) -> list[Step]:
        rows = self._conn.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [self._row_to_step(row) for row in rows]

    def next_seq(self, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) AS m FROM steps WHERE run_id = ?", (run_id,)
        ).fetchone()
        return int(row["m"]) + 1

    def find_responses_by_fingerprint(self, fingerprint: str) -> list[tuple[str, int]]:
        """跨运行查找命中 (run_id, seq)，按时间倒序。用于"换一个运行也能命中缓存"。"""
        rows = self._conn.execute(
            "SELECT run_id, seq FROM steps WHERE fingerprint = ? AND kind = 'llm'"
            " AND status = 'ok' ORDER BY created_at DESC",
            (fingerprint,),
        ).fetchall()
        return [(row["run_id"], row["seq"]) for row in rows]

    # ----------------------------------------------------------------- effects
    def append_effect(self, effect: Effect, run_id: str, seq: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO effects (run_id, seq, name, key, value, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, seq, effect.name, effect.key, _dumps(effect.value), utcnow_iso()),
        )
        self._conn.commit()

    def lookup_effect(self, name: str, key: str) -> tuple[bool, Any]:
        """跨运行按 (name, key) 命中副作用值。"""
        row = self._conn.execute(
            "SELECT value FROM effects WHERE name = ? AND key = ? ORDER BY created_at DESC LIMIT 1",
            (name, key),
        ).fetchone()
        if row is None:
            return False, None
        return True, _loads(row["value"])

    def iter_effects(self, run_id: str) -> Iterable[tuple[int, Effect]]:
        rows = self._conn.execute(
            "SELECT * FROM effects WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        for row in rows:
            yield int(row["seq"]), Effect(
                name=row["name"], key=row["key"], value=_loads(row["value"])
            )

    # ----------------------------------------------------------------- lineage
    def lineage(self, run_id: str) -> list[Run]:
        """从根到该运行的谱系（用于展示"这条分支从哪一步分出来"）。"""
        chain: list[Run] = []
        current = self.get_run(run_id)
        while current is not None:
            chain.append(current)
            current = (
                self.get_run(current.parent_run_id) if current.parent_run_id is not None else None
            )
        return list(reversed(chain))

    def stats(self) -> dict[str, int]:
        runs = self._conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        steps = self._conn.execute("SELECT COUNT(*) AS c FROM steps").fetchone()["c"]
        llm = self._conn.execute("SELECT COUNT(*) AS c FROM steps WHERE kind = 'llm'").fetchone()[
            "c"
        ]
        eff = self._conn.execute("SELECT COUNT(*) AS c FROM effects").fetchone()["c"]
        return {"runs": int(runs), "steps": int(steps), "llm_steps": int(llm), "effects": int(eff)}
