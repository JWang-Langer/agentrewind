# 安全策略

## 支持的版本

| 版本 | 支持状态 |
|---|---|
| 0.1.x | 支持（安全修复会以补丁版本发布） |

## 如何报告安全问题

**请不要开公开 Issue。** 请通过以下私有渠道报告：

- 邮箱：`security@agentrewind.invalid`（部署时替换为正式邮箱）
- 或使用 GitHub 的 [Private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)

报告请包含：影响版本、复现步骤、影响评估，以及（如有可能）最小复现仓库。

我们会在 **5 个工作日内**确认收到，并在确认修复方案后与您商定披露时间。

## 需要特别关注的风险面

AgentRewind 会**持久化 LLM 交互的完整请求与响应**。这是设计使然（否则无法确定性重放），
但也意味着以下风险必须被使用者知晓：

| 风险 | 说明 | 本项目提供的缓解 |
|---|---|---|
| 敏感数据落盘 | prompt 与响应可能含 PII、密钥、业务数据，全部写入本地 SQLite | 库文件默认位于 `.agentrewind/` 且已被 `.gitignore` 排除；请勿把库文件提交或共享 |
| 副作用被标记为可复用 | 回放返回的是**录制的**副作用值，不会执行真实操作 | 这是确定性所必需；对涉及资金/权限的副作用，请在 producer 中显式校验并单独审计 |
| 上游凭据处理 | CLI 通过 `--api-key` 或环境变量读取 | 命令行参数会出现在进程列表；生产环境请使用环境变量 `ARW_API_KEY` |
| 回放掩盖上游变更 | 上游若改变了对同一请求的返回，回放仍返回录制值 | 提供 `--on-divergence live` 以显式观察漂移 |

## 不在范围内

- 使用者自行在 prompt 中放入了密钥（请在应用层解决，不要依赖本库过滤）；
- 因把事件日志库文件公开共享导致的泄露。

## 加固建议

```bash
# 1) 库文件不要入库（默认已在 .gitignore）
echo ".agentrewind/" >> .gitignore

# 2) 用环境变量传凭据，而非命令行参数
export ARW_API_KEY=***
arw record --agent my_agent:main --base-url https://api.example.com/v1

# 3) 定期清理不再需要的运行记录（保留基线即可）
rm -f .agentrewind/arw.db-wal
```
