## 这个 PR 做了什么

<!-- 一句话说明；一个 PR 只做一件事 -->

## 为什么

<!-- 关联 Issue：Fixes #  -->

## 如何验证

<!-- 贴出你实际跑过的命令与输出，不要只写"已测试" -->

```bash
python -m unittest discover -s tests -t .
ruff check . && black --check . && mypy src
```

## 自检清单

- [ ] 新增/修改的行为带有测试，且测试断言的是**行为契约**（不是"某函数被调用过"）
- [ ] 涉及网络的测试使用 `agentrewind.testing.stub_server`，未引入 mock 框架
- [ ] 无裸 `except:`；捕获异常写明类型与理由
- [ ] 如行为有变更，已更新 `CHANGELOG.md`
- [ ] 如改动涉及**指纹规范化规则**，已提升 `fingerprint.SCHEMA_VERSION` 并说明兼容策略
- [ ] 如改动涉及**副作用语义或设计取舍**，已在 `docs/design-notes.md` 记录
- [ ] 提交已用 `git commit -s` 签署 DCO
- [ ] 文档与代码一致（命令、参数名、输出示例）

## 破坏性变更

- [ ] 无
- [ ] 有（请说明影响范围与迁移方法）
