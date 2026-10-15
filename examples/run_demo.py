# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""端到端演示脚本（也是演示视频的分镜脚本）。

它只调用**真实的** arw 命令行，不直接 import 内部函数——这样演示的就是用户
真正会用的东西。

跑法:  ./.venv/Scripts/python.exe examples/run_demo.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
if not Path(PY).exists():  # 非 Windows 或未建 venv 时退回当前解释器
    PY = sys.executable


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def _extract_last_json(text: str) -> dict:
    """从命令输出里取出**最后一个完整的 JSON 对象**（输出是美化多行的，不能按行解析）。"""
    decoder = json.JSONDecoder()
    best: dict = {}
    idx = text.find("{")
    while idx != -1:
        try:
            obj, end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            best = obj
        idx = text.find("{", idx + end)
    return best


def arw(*args: str, env: dict[str, str]) -> tuple[int, dict]:
    proc = subprocess.run(
        [PY, "-m", "agentrewind", *args],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
        check=False,
    )
    print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print("[stderr] " + proc.stderr.strip())
    return proc.returncode, _extract_last_json(proc.stdout)


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="arw-demo-")
    store = str(Path(workdir) / "demo.db")
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{ROOT / 'src'}{os.pathsep}{ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"

    # 启动被回放的对象：一个本地 stub 上游（真实 HTTP 服务）
    sys.path.insert(0, str(ROOT / "src"))
    from agentrewind.testing.stub_server import start_stub  # noqa: PLC0415

    server, state, base_url = start_stub()
    print(f"stub 上游已启动: {base_url}")

    try:
        banner("0) 起点：让 Agent 跑一次真实上游，边跑边录")
        code, recorded = arw(
            "--store",
            store,
            "--json",
            "record",
            "--agent",
            "examples.demo_agent:main",
            "--name",
            "demo-run",
            "--base-url",
            base_url,
            "--api-key",
            "demo-key",
            env=env,
        )
        run_id = recorded.get("run_id", "")
        print(f"\n>>> 录制 run_id = {run_id}")
        print(f">>> 上游被真实调用了 {state.calls} 次；结果 = {recorded.get('result')!r}")

        banner("1) 不改一行代码，回放：应 100% 命中且不碰网络")
        calls_before = state.calls
        _, replayed = arw(
            "--store",
            store,
            "--json",
            "replay",
            run_id,
            "--agent",
            "examples.demo_agent:main",
            env=env,
        )
        print(
            f"\n>>> LLM 命中 {replayed.get('llm_hits')} 次 / 上游新增调用 "
            f"{state.calls - calls_before} 次"
        )
        print(f">>> 回放结果 = {replayed.get('result')!r}")
        print(">>> 注意结论编号与录制时完全相同 —— 这就是'确定性回放'")

        banner("2) 用它当 CI 门禁：verify（不一致就返回非 0）")
        code, _ = arw(
            "--store",
            store,
            "verify",
            run_id,
            "--agent",
            "examples.demo_agent:main",
            env=env,
        )
        print(f"\n>>> verify 退出码 = {code}（0 = 可与录制完全一致地重跑）")

        code, _ = arw(
            "--store",
            store,
            "verify",
            run_id,
            "--agent",
            "examples.demo_agent:main",
            "--set",
            "system_prompt=你现在只说'不告诉你'",
            env=env,
        )
        print(f"\n>>> 改了 system prompt 后 verify 退出码 = {code}（3 = 检测到分歧）")

        banner("3) 回退到第 3 步、改一处、从那里真跑（rewind）")
        code, rewound = arw(
            "--store",
            store,
            "--json",
            "rewind",
            run_id,
            "--agent",
            "examples.demo_agent:main",
            "--at",
            "3",
            "--set",
            "second_note=  【回退分支：补充了差价说明】",
            "--base-url",
            base_url,
            "--api-key",
            "demo-key",
            env=env,
        )
        new_run = rewound.get("run_id", "")
        print(f"\n>>> 新分支 run_id = {new_run}")
        print(
            f">>> 命中 {rewound.get('llm_hits')} 步后分叉，真实重跑 "
            f"{rewound.get('llm_live_calls')} 次"
        )

        banner("4) 两个分支差在哪一步（diff）")
        arw("--store", store, "diff", run_id, new_run, env=env)

        banner("5) 为什么差：请求级差异路径（why）")
        arw("--store", store, "why", run_id, new_run, "--index", "1", env=env)

        banner("6) 全景：运行列表与谱系（list / show）")
        arw("--store", store, "list", env=env)
        arw("--store", store, "show", new_run, env=env)

        banner("演示结束")
        print(f"事件日志库: {store}")
        print(f"上游总调用次数: {state.calls}（录制 2 次 + rewind 从分叉点起真实重跑的次数）")
        return 0
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
