# cnlc-agentscope

测井解释智能体，技术设计见 `docs/03-system-architecture.md`。

当前实现 **Task 01：可运行工程骨架**（架构文档第 76–77 节）。
三个 Agent 目前为 Python 调用骨架；井数据、专业结果、模型响应均为显式 Mock。
AgentScope 2.x Runtime、内部统一模型、PostgreSQL、Redis、OpenTelemetry 导出和 Web
将在后续任务接入。本轮演示成功不代表 V0.1 MVP 已验收。

## 快速运行

需要 Python 3.11 和 uv，在仓库根目录执行：

```bash
uv sync --locked --dev
uv run cnlc-agent --well-id WELL_MOCK_001
```

也可以使用 `uv run python -m cnlc_agent.main`。
默认不需要 `.env`、密钥、数据库或外部网络连接。可复制 `.env.example` 到 `.env` 调整配置。

每次运行生成独立任务，输出目录为 `outputs/<task_id>/`：

- `result.json`：结构化结果、状态变化、步骤记录、缺失信息和错误。
- `report.md`：由同一份 State 渲染的骨架演示报告；失败任务输出诊断摘要。
- 标准错误输出：包含 task_id、trace_id 的 JSON 格式运行事件。

指定数据目录或输出目录：

```bash
uv run cnlc-agent --data-dir mock_data --output-dir outputs --well-id WELL_MOCK_001
```

退出码：`0` 表示演示链路 SUCCESS/WARNING，`1` 表示 FAILED/BLOCKED/REVIEW_REQUIRED，
`2` 表示配置、输入或输出错误。只有 Mock 模式可用；不支持的模式会明确报错。

## 验证

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
```

## 模块边界

| 目录 | 职责 |
|---|---|
| `domain/` | 最小数据契约、统一状态和错误类型，无框架/基础设施依赖 |
| `agents/` | MainAgent 启动流程，InterpretationAgent 处理 W06/W07，ValidationAgent 处理 W09 |
| `workflows/` | W01–W10 顺序、前置条件、状态修改、终止分支 |
| `tools/` | 输入/输出契约、超时和错误封装、Mock Tool |
| `application/` | 接口与装配，任务与报告持久化协调 |
| `infrastructure/` | Mock Fixture 读取、内存演示适配器、日志 Trace |
| `reports/` | State 转 JSON/Markdown，无新专业结论 |
| `config/` | 环境配置与敏感字段占位 |

详细执行范围、测试记录、未完成内容和后续依赖见
[`docs/tasks/001-project-skeleton.md`](docs/tasks/001-project-skeleton.md)。
Mock 格式说明见 [`mock_data/README.md`](mock_data/README.md)。

## Task 02：契约校验

已增加当前数据契约的 8 份 JSON Schema、独立校验命令、生成一致性检查和工具返回值校验。
正式业务 Schema 仍待项目方样例确认，详见
[`docs/04-data-tool-contracts.md`](docs/04-data-tool-contracts.md)。

```bash
uv run python -m cnlc_agent.schema export --check
uv run python -m cnlc_agent.schema validate mock-fixture mock_data/WELL_MOCK_001.json
```

任务结果见 [`docs/tasks/002-contract-validation.md`](docs/tasks/002-contract-validation.md)。
