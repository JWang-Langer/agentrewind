# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""arw 命令行：record / replay / verify / rewind / fork / diff / why / list / show / stats。"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import os
import sys
from collections.abc import Callable
from typing import Any

from agentrewind.branch import diff_runs, fork_run
from agentrewind.clients import OpenAICompatClient
from agentrewind.fingerprint import fingerprint_diff, request_fingerprint
from agentrewind.models import LLMRequest
from agentrewind.recorder import Divergence, Recorder, Replayer, Session
from agentrewind.store import Store

DEFAULT_STORE = os.environ.get("ARW_STORE", ".agentrewind/arw.db")
DEFAULT_BASE_URL = os.environ.get("ARW_BASE_URL", os.environ.get("OPENAI_BASE_URL", ""))


# --------------------------------------------------------------------- helpers
def _load_agent(spec: str) -> Callable[..., Any]:
    if ":" not in spec:
        raise SystemExit(f"--agent 需要 'module:function' 形式，收到: {spec!r}")
    module_name, func_name = spec.split(":", 1)
    sys.path.insert(0, os.getcwd())
    module = importlib.import_module(module_name)
    try:
        return getattr(module, func_name)  # type: ignore[no-any-return]
    except AttributeError as exc:
        raise SystemExit(f"{module_name} 里没有 {func_name}") from exc


async def _invoke(agent: Callable[..., Any], session: Session) -> Any:
    result = agent(session)
    if inspect.isawaitable(result):
        return await result
    return result


def _parse_mutations(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in pairs or []:
        if "=" not in raw:
            raise SystemExit(f"--set 需要 k=v 形式，收到: {raw!r}")
        key, value = raw.split("=", 1)
        out[key] = value
    return out


def _client(args: argparse.Namespace) -> OpenAICompatClient:
    base_url = args.base_url or DEFAULT_BASE_URL
    if not base_url:
        raise SystemExit("缺少上游地址：--base-url 或环境变量 OPENAI_BASE_URL / ARW_BASE_URL")
    api_key = (
        args.api_key or os.environ.get("OPENAI_API_KEY", "") or os.environ.get("ARW_API_KEY", "")
    )
    return OpenAICompatClient(base_url=base_url, api_key=api_key, timeout=args.timeout)


def _emit(payload: dict[str, Any], as_json: bool, human_lines: list[str]) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for line in human_lines:
            print(line)


def _resolve(store: Store, ref: str) -> str:
    run = store.resolve_run(ref)
    if run is None:
        raise SystemExit(f"找不到运行: {ref}")
    return run.id


# ------------------------------------------------------------------- commands
async def cmd_record(args: argparse.Namespace) -> int:
    agent = _load_agent(args.agent)
    mutations = _parse_mutations(args.set)
    with Store(args.store) as store:
        run = store.create_run(
            name=args.name or "record",
            mode="record",
            meta={"agent": args.agent, "mutations": mutations},
        )
        session = Recorder(store, run, client=_client(args), mutations=mutations)
        try:
            result = await _invoke(agent, session)
            status = "finished"
            exit_code = 0
        except Exception as exc:  # noqa: BLE001 - 记录失败并原样暴露
            result = None
            status = "failed"
            exit_code = 1
            print(f"[record] Agent 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            store.finish_run(run.id, status)
        summary = session.summary() | {"status": status, "result": result}
        await _maybe_close(session)
        _emit(
            summary,
            args.json,
            [
                f"运行 {run.id}（{run.name}）已录制，状态={status}",
                f"  步数 {session.seq}｜LLM 调用 {session.live_calls} 次"
                f"｜副作用 {session.effect_live} 次",
                f"  结果: {result!r}",
                f"  库文件: {args.store}",
                f"  下一步: arw replay {run.id} --agent {args.agent}",
            ],
        )
        return exit_code


async def cmd_replay(args: argparse.Namespace) -> int:
    agent = _load_agent(args.agent) if args.agent else None
    if agent is None:
        raise SystemExit("replay 需要 --agent（回放要重跑 Agent 代码，只是 LLM 调用走录制）")
    mutations = _parse_mutations(args.set)
    client = _client(args) if args.on_divergence == "live" else None
    with Store(args.store) as store:
        source_id = _resolve(store, args.run)
        source = store.get_run(source_id)
        run = store.create_run(
            name=args.name or f"replay:{source.name if source else source_id}",
            mode="replay" if args.on_divergence == "raise" else "rewind",
            meta={"source_run": source_id, "agent": args.agent, "mutations": mutations},
            parent_run_id=source_id,
            parent_seq=0,
        )
        session = Replayer(
            store,
            run,
            source_run_id=source_id,
            client=client,
            on_divergence=args.on_divergence,
            mutations=mutations,
        )
        status, exit_code = "finished", 0
        try:
            result = await _invoke(agent, session)
        except Divergence as exc:
            result = None
            status = "diverged"
            exit_code = 3 if args.on_divergence == "raise" else 1
            print(f"[replay] 检测到分歧（这是分叉点）：{exc}", file=sys.stderr)
            for item in exc.diffs[:10]:
                print(
                    f"    {item['kind']:<8} {item['path']}: {item['left']!r} -> {item['right']!r}"
                )
        else:
            status = "replayed"
        finally:
            store.finish_run(run.id, status)
            await _maybe_close(session)

        summary = session.summary() | {"status": status, "result": result}
        lines = [
            f"回放完成：源运行 {source_id}",
            f"  新运行 {run.id} ｜ 命中 {session.hits}/{session.expected_llm_steps} 次 LLM 调用"
            f"｜实际调用上游 {session.live_calls} 次",
            f"  副作用命中 {session.effect_hits} 次，真实执行 {session.effect_live} 次",
        ]
        if session.divergence:
            lines.append(f"  分叉点：第 {session.divergence.seq} 步（{session.divergence.kind}）")
        if session.hits == session.expected_llm_steps and session.divergence is None:
            lines.append("  结论：全步骤指纹一致，回放是确定性的（未产生任何上游调用）")
        lines.append(f"  下一步: arw diff {source_id} {run.id}")
        _emit(summary, args.json, lines)
        return exit_code


async def cmd_verify(args: argparse.Namespace) -> int:
    """CI 门禁：回放必须 100% 命中，否则退出码非 0。"""
    args.on_divergence = "raise"
    args.set = args.set or []
    args.name = f"verify:{args.run}"
    args.json = args.json
    return await cmd_replay(args)


async def _maybe_close(session: Session) -> None:
    client = session.client
    if client is not None and hasattr(client, "aclose"):
        await client.aclose()


def cmd_fork(args: argparse.Namespace) -> int:
    with Store(args.store) as store:
        run_id = _resolve(store, args.run)
        new_run = fork_run(store, run_id, args.at, name=args.name)
        _emit(
            {"run_id": new_run.id, "parent": run_id, "upto_seq": args.at},
            args.json,
            [
                f"已分叉：{new_run.id}（{new_run.name}）",
                f"  来源 {run_id} 的前 {args.at} 步已复制",
                f"  下一步: arw diff {run_id} {new_run.id}",
            ],
        )
        return 0


def cmd_diff(args: argparse.Namespace) -> int:
    with Store(args.store) as store:
        left, right = _resolve(store, args.left), _resolve(store, args.right)
        report = diff_runs(store, left, right, limit=args.limit)
        lines = [
            f"差异报告: {left}  vs  {right}",
            f"  步数 {report['left_steps']} vs {report['right_steps']}"
            f"｜分叉点 seq={report['fork_at_seq']}"
            f"｜不同步数 {report['diverged_steps']}",
            f"  token {report['left_tokens']} vs {report['right_tokens']}"
            f"（Δ{report['token_delta']:+d}）"
            f"｜耗时 {report['left_latency_ms']:.0f}ms vs {report['right_latency_ms']:.0f}ms",
        ]
        for item in report["details"]:
            lines.append(
                f"    seq {item['seq']:<3} {item['status']:<10} {item['kind']}"
                f"  工具 {item['left_tool_calls']} -> {item['right_tool_calls']}"
            )
            for diff_line in item["text_delta"][:6]:
                lines.append(f"        {diff_line}")
        _emit(report, args.json, lines)
        return 0


def cmd_why(args: argparse.Namespace) -> int:
    """解释两次运行的某一步为什么不同：打印请求级差异路径。"""
    with Store(args.store) as store:
        left, right = _resolve(store, args.left), _resolve(store, args.right)
        steps_l = [s for s in store.get_steps(left) if s.kind == "llm"]
        steps_r = [s for s in store.get_steps(right) if s.kind == "llm"]
        if args.index >= len(steps_l) or args.index >= len(steps_r):
            raise SystemExit(f"LLM 步索引越界：{len(steps_l)} vs {len(steps_r)}")
        a, b = steps_l[args.index], steps_r[args.index]
        req_a = LLMRequest(**a.request) if a.request else None
        req_b = LLMRequest(**b.request) if b.request else None
        if req_a is None or req_b is None:
            raise SystemExit("该步没有请求体")
        diffs = fingerprint_diff(req_a, req_b)
        payload = {
            "index": args.index,
            "left_fingerprint": request_fingerprint(req_a),
            "right_fingerprint": request_fingerprint(req_b),
            "diffs": diffs,
        }
        lines = [
            f"第 {args.index} 次 LLM 调用："
            f"{request_fingerprint(req_a)[:12]} vs {request_fingerprint(req_b)[:12]}",
            f"  差异 {len(diffs)} 处",
        ]
        for item in diffs[:20]:
            lines.append(
                f"    {item['kind']:<8} {item['path']}: {item['left']!r} -> {item['right']!r}"
            )
        _emit(payload, args.json, lines)
        return 0


def cmd_list(args: argparse.Namespace) -> int:
    with Store(args.store) as store:
        runs = store.list_runs(limit=args.limit)
        rows: list[dict[str, Any]] = [
            {
                "id": r.id,
                "name": r.name,
                "mode": r.mode,
                "status": r.status,
                "steps": len(store.get_steps(r.id)),
                "parent": r.parent_run_id,
                "created_at": r.created_at,
            }
            for r in runs
        ]
        lines = [f"{'id':<28}{'name':<28}{'mode':<8}{'status':<10}steps"]
        for row in rows:
            name = row["name"] if len(row["name"]) <= 26 else row["name"][:25] + "…"
            lines.append(
                f"{row['id']:<28}{name:<28}{row['mode']:<8}{row['status']:<10}{row['steps']}"
            )
        _emit({"runs": rows}, args.json, lines)
        return 0


def cmd_show(args: argparse.Namespace) -> int:
    with Store(args.store) as store:
        run_id = _resolve(store, args.run)
        run = store.get_run(run_id)
        steps = store.get_steps(run_id)
        payload: dict[str, Any] = {
            "run": run.model_dump(mode="json") if run else None,
            "lineage": [r.id for r in store.lineage(run_id)],
            "steps": [
                {
                    "seq": s.seq,
                    "kind": s.kind,
                    "fingerprint": s.fingerprint,
                    "effect": [s.effect_name, s.effect_key],
                    "status": s.status,
                    "duration_ms": s.duration_ms,
                    "text": ((s.response or {}).get("text") or "")[:80],
                    "tool_calls": [c["name"] for c in ((s.response or {}).get("tool_calls") or [])],
                }
                for s in steps
            ],
        }
        lines = [f"{run_id}（{run.name if run else '?'}） 谱系 {' <- '.join(payload['lineage'])}"]
        for item in payload["steps"]:
            fp = (item["fingerprint"] or "-")[:12]
            extra = ""
            if item["kind"] == "llm":
                extra = f"tools={item['tool_calls']} text={item['text']!r}"
            elif item["kind"] == "effect":
                extra = f"{item['effect']}"
            else:
                extra = f"{item['text']!r}"
            lines.append(f"  seq {item['seq']:<3} {item['kind']:<7} fp={fp:<14}{extra}")
        _emit(payload, args.json, lines)
        return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with Store(args.store) as store:
        stats = store.stats()
        _emit(stats, args.json, [f"{k}: {v}" for k, v in stats.items()])
        return 0


# --------------------------------------------------------------------- argparse
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arw",
        description="AgentRewind —— Agent 的确定性录制 / 回放 / 回退 / 差异定位",
    )
    parser.add_argument(
        "--store", default=DEFAULT_STORE, help="事件日志库文件（默认 .agentrewind/arw.db）"
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（便于 CI 与二次处理）")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_agent_args(p: argparse.ArgumentParser, need_agent: bool = True) -> None:
        p.add_argument("--agent", required=need_agent, help="Agent 入口 module:function")
        p.add_argument(
            "--set",
            action="append",
            help="注入改动 k=v（可多次），Agent 用 session.mutation(k) 读取",
        )
        p.add_argument(
            "--base-url", default=None, help="上游 base_url（如 http://127.0.0.1:8000/v1）"
        )
        p.add_argument("--api-key", default=None, help="上游 api key（也可用环境变量 ARW_API_KEY）")
        p.add_argument("--timeout", type=float, default=120.0)

    p_record = sub.add_parser("record", help="真实执行并录制")
    add_agent_args(p_record)
    p_record.add_argument("--name", default=None)
    p_record.set_defaults(func=cmd_record, coro=True)

    p_replay = sub.add_parser("replay", help="确定性回放（不改代码时应 100%% 命中）")
    add_agent_args(p_replay)
    p_replay.add_argument("run", help="源运行 id 或前缀")
    p_replay.add_argument("--name", default=None)
    p_replay.add_argument(
        "--on-divergence",
        choices=["raise", "live"],
        default="raise",
        help="首处指纹不一致时：raise=报错停止；live=从该步起真实调用并继续（即回退分叉）",
    )
    p_replay.set_defaults(func=cmd_replay, coro=True)

    p_verify = sub.add_parser("verify", help="CI 门禁：回放必须 100%% 命中")
    add_agent_args(p_verify)
    p_verify.add_argument("run")
    p_verify.set_defaults(func=cmd_verify, coro=True)

    p_rewind = sub.add_parser(
        "rewind", help="回退到某一步并改一改（等价于 replay --on-divergence live）"
    )
    add_agent_args(p_rewind)
    p_rewind.add_argument("run")
    p_rewind.add_argument(
        "--at", type=int, help="提示用：期望的分叉步号（仅用于打印，不影响控制流）"
    )
    p_rewind.set_defaults(func=cmd_replay, coro=True, name=None)

    p_fork = sub.add_parser("fork", help="离线分叉：复制前 N 步成新运行（不调用上游）")
    p_fork.add_argument("run")
    p_fork.add_argument("--at", type=int, required=True)
    p_fork.add_argument("--name", default=None)
    p_fork.set_defaults(func=cmd_fork, coro=False)

    p_diff = sub.add_parser("diff", help="两条运行的差异报告")
    p_diff.add_argument("left")
    p_diff.add_argument("right")
    p_diff.add_argument("--limit", type=int, default=20)
    p_diff.set_defaults(func=cmd_diff, coro=False)

    p_why = sub.add_parser("why", help="解释某一步为什么不同（请求级差异路径）")
    p_why.add_argument("left")
    p_why.add_argument("right")
    p_why.add_argument("--index", type=int, default=0, help="第几次 LLM 调用（从 0 起）")
    p_why.set_defaults(func=cmd_why, coro=False)

    p_list = sub.add_parser("list", help="列出运行")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=cmd_list, coro=False)

    p_show = sub.add_parser("show", help="查看某次运行的步骤")
    p_show.add_argument("run")
    p_show.set_defaults(func=cmd_show, coro=False)

    p_stats = sub.add_parser("stats", help="库统计")
    p_stats.set_defaults(func=cmd_stats, coro=False)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "coro", False):
        if args.command == "rewind":
            args.on_divergence = "live"
            if not args.agent:
                raise SystemExit("rewind 需要 --agent")
        return int(asyncio.run(args.func(args)))
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
