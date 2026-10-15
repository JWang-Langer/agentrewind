# AgentRewind

**框架无关的 LLM Agent 确定性 录制 / 回放 / 回退 / 差异定位。**

Agent 出了 bug，你改了 prompt 想验证，却再也复现不出当时那一次运行——因为 LLM 不吃 seed、
工具调用带副作用、字典键序悄悄变。AgentRewind 把 LLM 调用层变成**可确定性重放**的一等公民，
让 Agent 的"随机"行为像普通程序一样被复现、回退、逐步骤比对。

```bash
pip install agentrewind          # 或 pip install -e . 从源码安装
arw record --agent my_agent:main --name baseline      # 真跑一遍，边跑边录
arw verify <run_id> --agent my_agent:main            # CI 门禁：必须逐位一致重跑
arw rewind <run_id> --agent my_agent:main --set system_prompt="换一版"   # 回退到分叉点并真跑
arw diff   <run_a> <run_b>                            # 两条分支差在哪一步、代价差多少
arw why    <run_a> <run_b> --index 1                  # 为什么差：请求级差异路径
```

## 它解决什么问题

| 现实痛点 | AgentRewind 的答案 |
|---|---|
| LLM 输出随机，bug 复现不了 | 请求指纹 + 响应原文落库，回放返回**录制时的同一份响应** |
| 重跑一遍要花钱、还未必一样 | 回放**零上游调用**；CI 里可以放心跑回归 |
| 改了一行 prompt，不知道影响哪一步 | 逐步骤指纹比对，**精确报告首个分歧步**与请求级差异路径 |
| 工具/DB/HTTP 副作用被重复执行 | 副作用以 `(name, key)` 幂等录制，回放时**不执行真实副作用** |
| "如果当初在第 N 步换一种做法会怎样" | `rewind` 从第 N 步起真实重跑，形成可对比的分支与谱系 |

## 核心机制（四件套）

1. **请求指纹**：`SCHEMA_VERSION + 规范化 JSON(请求)` → SHA-256 前 128 位。
   规范化 = 键排序、紧凑分隔符、保留 Unicode；**保留 None**（`temperature=None` 与 `0.0` 语义不同）。
2. **事件日志**：SQLite（WAL）三张表 `runs / steps / effects`，每步写入即提交，Agent 崩溃不丢已发生步骤。
   payload **逐字节忠实往返**（不排序键）——这条是踩过坑才定下来的，见下文。
3. **副作用幂等通道**：`session.effect(name, key, producer)`，回放命中则直接返回录制值、**不执行** producer。
4. **分叉与差异**：回放时逐条比对指纹，**首个不一致即分叉点**；
   `diff` 输出分叉步号、工具调用变化、文本增量、token/耗时差；`why` 输出到 `messages[i].content` 级的差异路径。

## 与同类方案的差异

| 方案 | 做什么 | 不做什么 |
|---|---|---|
| LangSmith / Langfuse / OpenTelemetry trace | **观测**：把发生过的事展示出来 | 不能把运行**重新执行**成同一结果 |
| LangGraph checkpoint / time travel | 在 LangGraph 内从检查点恢复、可改状态 | 恢复后 LLM 调用仍会**重新采样**，且绑定单一框架 |
| 录制回放型 mocking 库 | 替换函数、断言被调用 | 不做"请求指纹 → 首次分歧定位 → 分支谱系" |
| **AgentRewind** | 指纹 + 忠实往返 + 副作用幂等 ⇒ **确定性重放**；分叉与差异定位；框架无关（任何 OpenAI 兼容上游） | 不做可视化大盘（那是观测层的事） |

详细对比与取舍见 [`docs/prior-art.md`](docs/prior-art.md)。

## 一次真实运行的实测输出

```
0) 录制：上游被真实调用 2 次；结果 = 'SKU-1 价格 199.00 元。结论编号[829427182]'
1) 回放：LLM 命中 2 次 / 上游新增调用 0 次；结果 = 'SKU-1 价格 199.00 元。结论编号[829427182]'
   ↑ 结论编号与录制完全相同 —— 这就是确定性
2) verify 退出码 = 0（一致）；改 system prompt 后 = 3（检测到分歧）
3) rewind --at 3 --set second_note=... ：命中 1 步后在第 3 步分叉，真实重跑 1 次
4) diff ：分叉点 seq=3｜不同步数 1｜token 305 vs 305
5) why  ：changed messages[3].content: '{"currency":"CNY",...}' -> '{"currency":"CNY",...}  【回退分支…】'
```

复现整段（含一个带随机 nonce 的本地 stub 上游，用来证明"回放真的没重新采样"）：

```bash
./.venv/Scripts/python.exe examples/run_demo.py     # Windows
# 或 python examples/run_demo.py
```

## 目录结构

```
agentrewind/
├── src/agentrewind/
│   ├── fingerprint.py     # 规范化序列化 + 请求指纹 + 差异路径
│   ├── models.py          # LLMRequest / LLMResponse / Step / Run / Effect
│   ├── store.py           # SQLite 事件日志（runs/steps/effects）
│   ├── recorder.py        # Session / Recorder / Replayer / Divergence
│   ├── branch.py          # 对齐、分叉定位、差异报告、离线 fork
│   ├── clients.py         # 只依赖 httpx 的 OpenAI 兼容异步客户端
│   ├── cli.py             # arw 命令行
│   └── testing/           # 随库发布的本地 stub 上游（供下游写回放测试）
├── examples/              # 示例 Agent + 端到端演示脚本
└── tests/                 # 35 个单元/集成测试（真实 HTTP，不使用 mock 框架）
```

## 测试

```bash
python -m unittest discover -s tests -t .     # 35 passed
```

测试断言的是**行为契约**，不是实现细节：

- 回放必须**逐位一致**（结果里的随机 nonce 与录制时相同）；
- 回放必须**零上游调用**（stub 计数不变）；
- 副作用在回放时**不得重复执行**（producer 计数不变）；
- 改 prompt / 多调一次 / 缺副作用，三种情况都必须被精确定位并被拒绝。

## 状态与路线图

- [x] v0.1 确定性录制 / 回放 / 回退 / 差异定位 / CLI / 35 测试
- [ ] 副作用自动拦截层（对 `httpx` / `open()` 的透明包装，免手写 `session.effect`）
- [ ] `stream=True` 流式响应的分块录制与回放
- [ ] 跨语言/跨进程的指纹兼容性测试夹具
- [ ] `arw bisect`：在两条分支之间自动二分定位引入行为变化的最小改动

详见 [`CHANGELOG.md`](CHANGELOG.md)。

## 贡献

见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。本项目遵循 [Contributor Covenant](CODE_OF_CONDUCT.md)，
安全问题请按 [`SECURITY.md`](SECURITY.md) 私下报告。

## 许可证

[Apache License 2.0](LICENSE)。第三方依赖清单与许可证审查见 [`docs/dependency-audit.md`](docs/dependency-audit.md)。
