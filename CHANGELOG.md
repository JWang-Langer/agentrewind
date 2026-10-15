# 变更日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 新增

- **副作用自动拦截层**（`intercept.py`）：`SideEffectTransport` 把 `httpx.AsyncClient` 的请求
  透明登记为副作用，键为 `METHOD URL body-sha256`（**含请求体**，避免不同载荷误命中）；
  回放命中时**不发出请求**，用录制值重建响应（状态码/头/body，体为 base64 往返）。
  支持 `passthrough_hosts` 白名单（把上游 LLM 的 host 排除，避免与指纹通道重复登记）。
  配套 7 个专项测试：录制真实发请求、回放零新增请求且响应逐字节一致、
  请求体变化不误命中并报 `effect_miss`、passthrough 不入副作用通道。
- `run_demo.py` 视频友好模式（`--video` / `--pause`）：隐藏原始 JSON、精简命令回显、关键结论后自动停顿。

### 计划

- 文件 I/O 的副作用拦截（`open()` / `Path.write_bytes` 包装）
- `stream=True` 流式响应的分块录制与回放
- 跨进程 / 跨语言指纹兼容性测试夹具
- `arw bisect`：在两条分支间自动二分定位引入行为变化的最小改动
- 大规模库（>10 万步）的性能基准与索引优化

## [0.1.0] - 2026-10-15

首个可用版本。核心目标是让"确定性重放"成为**可进 CI 的断言**，而不是一个口号。

### 新增

- **请求指纹**（`fingerprint.py`）：`SCHEMA_VERSION + 规范化 JSON` → SHA-256 取 128 位；
  规范化含键排序、紧凑分隔符、保留 Unicode 与 `None`（`temperature=None` 与 `0.0` 语义不同）。
- **差异路径解释**（`fingerprint_diff`）：给出到 `messages[i].content` 粒度的差异路径，
  用于回答"为什么两次请求不同"。
- **事件日志**（`store.py`）：SQLite（WAL）`runs / steps / effects` 三表；
  每步写入即提交；支持跨运行按指纹与 `(name, key)` 命中查询；ULID 主键；分支谱系查询。
- **会话层**（`recorder.py`）：`Recorder`（真实执行并录制）、
  `Replayer`（逐条比对指纹，首个不一致即分叉点）、`Divergence`（把"不一致"作为一等信息）。
- **副作用幂等通道**：`session.effect(name, key, producer)`；回放命中则返回录制值且**不执行** producer。
- **分支与差异**（`branch.py`）：`align_runs` / `first_divergence` / `diff_runs`（含分叉步号、
  工具调用变化、文本增量、token 与耗时差）/ `fork_run`（离线复制前 N 步）。
- **命令行**（`cli.py`）：`record / replay / verify / rewind / fork / diff / why / list / show / stats`，
  全部支持 `--json` 输出；`verify` 以退出码作为 CI 门禁。
- **测试工具**（`testing/`）：随库发布的本地 stub 上游（真实 HTTP 服务 + 随机 nonce，
  用于可观测地证明"回放没有重新采样"）。
- **示例**：`examples/demo_agent.py`（比价助手）、`examples/run_demo.py`（端到端演示脚本）。
- **文档**：README、`docs/design-notes.md`（含缺陷档案）、`docs/prior-art.md`、
  `docs/dependency-audit.md`。
- **治理**：Apache-2.0、CONTRIBUTING（含 DCO）、CODE_OF_CONDUCT、SECURITY、
  Issue/PR 模板、本变更日志。

### 修复（开发期发现，均在 0.1.0 内解决）

- **修复回放确定性缺陷**：存储层曾用 `sort_keys=True` 序列化 payload，
  导致副作用值的键序在存取之间被重排、prompt 字节改变、回放被误判为分歧。
  现改为逐字节忠实往返，并新增回归测试
  （`tests/test_store.py::test_effect_value_round_trip_preserves_key_order`）。
- **修复分叉点误报**：注释步（`session.note`）的内容差异不再计入分歧，
  改为单列 `note_differences` 计数，避免掩盖真正的分叉位置。
- **修复** `arw --help` 因帮助文本含裸 `%` 抛 `ValueError`。
- **修复** `diff` 报告访问不存在的 `Step.tool_calls` 属性。

### 已知限制

- 仅覆盖非流式 `/chat/completions`。
- 副作用需显式声明（自动拦截层在路线图中）。
- 未在超大规模（>10 万步）库上做性能验证。

[未发布]: https://github.com/JWang-Langer/agentrewind/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/JWang-Langer/agentrewind/releases/tag/v0.1.0
