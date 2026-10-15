# 贡献指南

感谢参与 AgentRewind。本项目的目标是"可被信任的确定性"，因此对**正确性与可验证性**的要求
高于对功能数量的要求。请先读本文再提 PR。

## 开发环境

```bash
git clone https://github.com/JWang-Langer/agentrewind.git
cd agentrewind
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"     # Windows
# source .venv/bin/activate && pip install -e ".[dev]"    # Linux/macOS
```

## 提交前必须通过

```bash
python -m unittest discover -s tests -t .     # 测试必须全绿
ruff check .
black --check .
mypy src
```

四条代码规范（本项目的硬性约定）：

1. `ruff` / `black`（行宽 100）/ `mypy --strict` 全部通过；
2. **不使用 mock 框架**：涉及网络的测试一律用 `agentrewind.testing.stub_server`
   （真实 HTTP 服务），以保证测的是真实行为；
3. 新增行为必须带测试，且测试要断言**行为契约**（如"回放零上游调用"），
   而不是"某函数被调用过"；
4. 禁止裸 `except:`；捕获异常必须写明类型与处理理由。

## 提交信息

采用 Conventional Commits：

```
feat(fingerprint): 支持多模态消息的指纹规范化
fix(store): payload 落库不再排序键（修复回放指纹漂移）
docs(design): 补缺陷档案 #1 的根因分析
test(replay): 增加 effect_miss 的拒绝执行用例
```

## PR 要求

- 一个 PR 只做一件事；说明"改了什么 / 为什么 / 如何验证"；
- 行为变更请同步更新 `CHANGELOG.md`（Keep a Changelog 格式）；
- 若改动涉及**指纹规范化规则**，必须同时提升 `fingerprint.SCHEMA_VERSION`
  并说明旧库的兼容策略——这会直接导致跨版本命中失效，属破坏性变更；
- 若改动涉及**副作用语义**，请在 `docs/design-notes.md` 记录取舍。

## 签署 DCO

本项目要求所有提交携带开发者原创声明（DCO 1.1）。提交时使用 `git commit -s`，
在提交信息末尾生成 `Signed-off-by: Your Name <you@example.com>`。
未签署的提交将无法合入。

## 报告问题

- 一般问题：使用 Issue 模板（Bug 报告 / 功能请求）；
- 安全问题：**不要**开公开 Issue，见 [`SECURITY.md`](../SECURITY.md)。

## 行为准则

参与本项目即表示同意遵守 [`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md)。
