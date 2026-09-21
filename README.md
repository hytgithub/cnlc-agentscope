# cnlc-agentscope

测井解释智能体，技术设计见 `docs/03-system-architecture.md`。

当前实现已完成 Task 01–03，并完成 Task 04 的通用 OpenAI-Compatible 模型 Gateway。
默认 provider 仍为 Mock；真实 provider 使用配置的 DashScope 北京兼容地址、模型名和本地 API Key。
AgentScope Runtime、Web、OpenTelemetry 导出和专业业务扩展仍属于后续任务，本轮不代表 V0.1 MVP 已验收。

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
`2` 表示配置、输入或输出错误。模型默认为 Mock；真实模式配置缺失会明确报错，不会静默回退。

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


## Task 03：PostgreSQL / Redis

设置 `CNLC_PERSISTENCE=postgres-redis` 后，任务通过真实 SQLAlchemy/asyncpg 与 redis-py
适配器保存；默认 memory 模式保留离线演示。连接、迁移、Compose、历史查询与测试命令见
[`docs/05-persistence.md`](docs/05-persistence.md)。

2026-09-21 用户提供本地真实服务测试结果：3 passed in 8.77s，补齐 Task 03 联调验证。
此前执行环境的普通测试为 49 passed、3 skipped；两次结果不合并声称为一次全量测试。
执行记录见 [`docs/tasks/003-persistence.md`](docs/tasks/003-persistence.md)。

## Task 04：公网真实模型

新增通用异步 `OpenAICompatibleModelGateway`，支持 DashScope 北京兼容接口：

```dotenv
CNLC_MODEL_PROVIDER=openai_compatible
MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MODEL_NAME=qwen-plus
MODEL_API_KEY=仅在本地环境设置
```

也可使用 `DASHSCOPE_API_KEY`。Gateway 对响应做 JSON object 校验，并对鉴权、限流、
超时、5xx、非法响应和网络错误做稳定分类；重试次数有限且不包含业务 Rollback。
默认离线测试不访问公网。显式配置 `CNLC_RUN_REAL_MODEL_TEST=1` 和本地 API Key 后，
运行 `uv run pytest tests/integration/test_real_model.py -v` 验证 qwen-plus 连通性。
实现和边界见 [`docs/tasks/004-real-model.md`](docs/tasks/004-real-model.md)。
