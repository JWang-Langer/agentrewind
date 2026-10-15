# Copyright 2026 WZMLab
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""AgentRewind —— 框架无关的 LLM Agent 确定性录制 / 回放 / 回退 / 差异定位。

核心主张：把 LLM 调用层变成**可确定性重放**的一等公民，从而让 Agent 的
"随机"行为可以像普通程序一样被复现、被回退、被逐步骤比对。
"""

from agentrewind.branch import align_runs, diff_runs, fork_run
from agentrewind.clients import LLMClient, OpenAICompatClient
from agentrewind.fingerprint import canonical_json, fingerprint_diff, request_fingerprint
from agentrewind.intercept import SideEffectTransport, effect_key, instrument
from agentrewind.models import Effect, LLMRequest, LLMResponse, Run, Step, ToolCall
from agentrewind.recorder import Divergence, Recorder, Replayer, Session
from agentrewind.store import Store
from agentrewind.ulid import new_ulid

__version__ = "0.1.0"

__all__ = [
    "Effect",
    "LLMClient",
    "LLMRequest",
    "LLMResponse",
    "Run",
    "Step",
    "ToolCall",
    "Session",
    "Recorder",
    "Replayer",
    "Divergence",
    "Store",
    "OpenAICompatClient",
    "SideEffectTransport",
    "instrument",
    "effect_key",
    "canonical_json",
    "request_fingerprint",
    "fingerprint_diff",
    "align_runs",
    "diff_runs",
    "fork_run",
    "new_ulid",
    "__version__",
]
