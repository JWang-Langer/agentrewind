# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""示例 Agent：一个最小的"比价助手"。

它是被测对象，也是演示脚本。约定：
- 只能通过 session.complete() 调 LLM、session.effect() 做副作用；
- 所有可变行为都从 session.mutation(name, default) 读取，
  这样"回退到某一步改一改"就变成了命令行上的 --set k=v。
"""

from __future__ import annotations

import json
from typing import Any

from agentrewind.fingerprint import canonical_json
from agentrewind.models import LLMRequest
from agentrewind.recorder import Session

DEFAULT_SYSTEM_PROMPT = (
    "你是比价助手。若用户询问价格，你必须先 LOOKUP 再回答；回答里给出 SKU 与价格。"
)

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "lookup_price",
            "description": "查询某个 SKU 的当前价格",
            "parameters": {
                "type": "object",
                "properties": {"sku": {"type": "string", "description": "商品编码"}},
                "required": ["sku"],
            },
        },
    }
]

_PRICE_TABLE = {"SKU-1": 199.0, "SKU-2": 259.0}


async def main(session: Session) -> str:
    model = session.mutation("model", "stub-model")
    system_prompt = session.mutation("system_prompt", DEFAULT_SYSTEM_PROMPT)
    user_goal = session.mutation("user_goal", "帮我看下 SKU-1 现在多少钱")
    second_note = session.mutation("second_note", "")
    session.note(f"goal={user_goal!r} second_note={second_note!r}")

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_goal},
    ]
    first = await session.complete(
        LLMRequest(model=model, messages=[dict(m) for m in messages], tools=TOOLS, temperature=0.0)
    )

    if not first.tool_calls:
        return first.text

    call = first.tool_calls[0]
    arguments = json.loads(call.arguments or "{}")
    sku = str(arguments.get("sku", "SKU-1"))

    price = await session.effect(
        "price_api",
        sku,
        lambda: {"sku": sku, "price": _PRICE_TABLE.get(sku, 0.0), "currency": "CNY"},
    )

    messages.append(
        {
            "role": "assistant",
            "content": first.text or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
            ],
        }
    )
    messages.append(
        {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            # 关键：把结构化数据放进 prompt 前必须做规范化序列化，
            # 否则字典插入顺序会变成"隐式非确定性"，回放时指纹必然漂移。
            "content": canonical_json(price) + second_note,
        }
    )

    second = await session.complete(
        LLMRequest(model=model, messages=[dict(m) for m in messages], tools=TOOLS, temperature=0.0)
    )
    return second.text
