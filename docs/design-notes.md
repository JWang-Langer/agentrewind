# 设计说明与缺陷档案

本文记录 AgentRewind 的设计取舍，以及开发过程中真实踩到、并已修复的缺陷。
写下来的理由很直接：**"确定性回放"这类能力，其价值完全取决于它是否真的确定**，
所以每一个曾经破坏确定性的坑都必须留档，作为后续回归测试的依据。

---

## 一、为什么"读日志"不够，必须做"可执行重放"

现有观测工具（LangSmith / Langfuse / OTel trace）把一次运行渲染成一条时间线，这对**看**很有用；
但对**改**没有用：你改完 prompt，仍然无法把当时的运行重新跑成同一结果，因为：

1. LLM 采样本身不确定（多数兼容接口的 `seed` 只是尽力而为，不保证跨版本一致）；
2. 工具与外部接口有副作用，重跑会重复扣费、重复写库；
3. 即使输入在语义上相同，字节也可能不同（见下文缺陷 #1）。

因此本项目的核心不是"记录"，而是**把不变量（invariant）变成可执行的东西**：
"同一串请求指纹，必须得到同一串响应"。可执行，才能进 CI。

---

## 二、关键设计决策

### 1. 指纹的输入必须"语义完备且字节确定"

`request_fingerprint()` 的输入是 `SCHEMA_VERSION + canonical_json(LLMRequest)`：

- **键排序 + 紧凑分隔符**：dict 插入顺序不影响指纹（否则同一次语义会算出不同指纹）；
- **保留 `None`**：`temperature=None`（用服务端默认）与 `temperature=0.0`（贪婪解码）是两种不同请求，
  丢弃 None 会制造"假命中"——这是最危险的一类错误，因为回放会返回一个**不适用于该请求**的响应；
- **版本前缀**：规范化规则一旦调整，旧指纹必须整体失效，否则跨版本库里会发生错配。

### 2. 存储必须逐字节忠实往返

payload 落库时**不排序键**。理由见缺陷 #1。

### 3. 回放遇到不一致时"报错"而不是"静默回落"

`on_divergence="raise"`（默认）会在首个指纹不一致处抛 `Divergence`。
若默认静默走真实调用，回放就退化成了一个普通的缓存，**确定性无声消失**，
而这恰恰是最难察觉的失败模式。需要"从分歧点继续真跑"时，必须显式选择 `--on-divergence live`。

### 4. 副作用：显式通道为默认，透明拦截为可选

`session.effect(name, key, producer)` 是**推荐且默认**的方式：显式声明副作用边界，语义清楚、可审计。

在此基础上，v0.1 追加了一个**可选**的透明拦截层 `intercept.instrument(session)`：
给 `httpx` 客户端挂上 `SideEffectTransport`，HTTP 请求/响应自动进确定性通道。三条设计约束：

1. **键必须含请求体哈希**（`METHOD URL body-sha256`）。只用 URL 会让"同一接口不同载荷"
   误命中，这是比不命中更危险的错误——回放会返回一个**不适用于该请求**的响应。
2. **提供 `passthrough_hosts`**。LLM 上游走指纹通道（`session.complete`），
   若同一请求被当成副作用再登记一次，会出现两套账本、互相矛盾。
3. **响应会被完整缓冲**，因此不支持流式副作用响应；需要流式时应回到显式 `session.effect`
   或在应用层处理。这个取舍是明确的，不隐藏。

覆盖范围目前是 **HTTP**；文件 I/O（`open()`）的拦截仍在路线图中，不在本版本声称范围内。

### 5. 注释步不参与确定性判定

Agent 常用 `session.note()` 写诊断信息（如本次注入的改动）。若把注释内容变化算作"分歧"，
真正的分叉点会被噪声掩盖（开发中确实出现了 `fork_at_seq` 被误报为 0 的情况）。
**判定规则：只有 llm 与 effect 步影响确定性；注释步仅用于展示。**

---

## 三、缺陷档案（真实发生、已修复、已加回归测试）

### 缺陷 #1：存储排序键破坏回放确定性 ★最有价值的一个

- **现象**：同一份代码、同一份输入，回放时在第 2 次 LLM 调用处报出 1 处差异：

  ```
  changed  messages[3].content
      recorded: '{"sku": "SKU-1", "price": 199.0, "currency": "CNY"}'
      replayed: '{"currency": "CNY", "price": 199.0, "sku": "SKU-1"}'
  ```

- **根因**：`Store._dumps()` 为方便 diff 使用了 `json.dumps(..., sort_keys=True)`。
  副作用值落库时被重排为字典序，回读后 `json.dumps(price)` 的输出与原运行的字节不同
  → prompt 字节变化 → 指纹变化 → 回放被判为分歧。
- **为什么值得记录**：这是一个**隐式非确定性**（implicit nondeterminism）的教科书案例。
  它不会报错、不会崩溃，只是在几天后让回放莫名其妙地失败。任何"录制-回放"系统都会遇到它。
- **修复**：存储序列化不排序键（`docs` 与源码注释中均标注原因），
  并在 `examples/demo_agent.py` 中改为把结构化数据经 `canonical_json()` 放进 prompt。
- **回归测试**：`tests/test_store.py::test_effect_value_round_trip_preserves_key_order`。
- **给使用者的规则**：**任何进入 prompt 的结构化数据都必须先做规范化序列化**，
  否则同一次运行换个进程就可能算出不同指纹。

### 缺陷 #2：注释步导致分叉点被误报

- **现象**：rewind 时注入的改动写在 note 里，`diff` 报告 `fork_at_seq=0`，实际分叉在第 3 步。
- **根因**：`align_runs()` 把 note 的内容差异当作分歧。
- **修复**：note 一律判为 `SAME`，其内容差异单列 `note_differences` 计数。
- **回归测试**：`tests/test_replay.py::test_diff_locates_fork_point` 中的 `note_differences` 断言。

### 缺陷 #3：`argparse` 帮助文本中的裸 `%`

- **现象**：`arw --help` 直接抛 `ValueError: badly formed help string`。
- **根因**：argparse 对 help 字符串做 `%` 格式化，裸 `%` 必须写成 `%%`。
- **修复**：`100%% 命中`。

### 缺陷 #4：`Step` 上没有 `tool_calls`

- **现象**：`diff` 抛 `AttributeError: 'Step' object has no attribute 'tool_calls'`。
- **根因**：工具调用在 `Step.response["tool_calls"]` 里，模型层并不暴露 `Step.tool_calls`。
- **修复**：`_tool_names(step)` 统一从响应取，避免调用方各自猜路径。

---

## 四、已知边界（不夸大）

- 只覆盖**非流式** `/chat/completions`；流式响应在路线图里。
- 副作用需手工声明（见上文决策 4）。
- 指纹是**请求级**的：服务端若改变了同一请求的返回结果（模型升级），
  回放仍会返回录制值——这既是特性（可复现），也是风险（可能掩盖上游变更）。
  使用 `arw replay --on-divergence live` 或对比不同日期的库可观察此漂移。
- SQLite 存储层是同步的（`sqlite3` 无异步驱动）；网络与 Agent 逻辑全异步，边界明确。
