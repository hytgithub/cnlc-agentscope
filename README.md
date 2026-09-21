# cnlc-agentscope

测井解释智能体，技术设计见 `docs/03-system-architecture.md`。

当前实现已完成 Task 01–06，并完成 Task 007 的 AgentScope 官方 Web UI 后端适配。
默认 provider 仍为 Mock；真实 provider 使用配置的 DashScope 北京兼容地址、模型名和本地 API Key。
官方前端源码不在本仓库；OpenTelemetry 导出和专业业务扩展仍属于后续任务，本轮不代表
V0.1 MVP 已验收。

## 快速运行

需要 Python 3.11 和 uv，在仓库根目录执行：

```bash
uv sync --locked --dev
uv run cnlc-agent --well-id WELL_MOCK_001
```

也可以使用 `uv run python -m cnlc_agent.main`。
默认不需要 `.env`、密钥、数据库或外部网络连接。可复制 `.env.example` 到 `.env` 调整配置。

明天的完整演示链路可显式启用 Demo Mode：

```bash
CNLC_MODE=demo CNLC_MODEL_PROVIDER=mock uv run cnlc-agent --well-id WELL_MOCK_001
```

Demo Mode 将演示资料视为完整，W03/W04/W05/W08 使用 fixture Mock Tool，W06/W07
仍通过统一 ModelGateway，W09 返回结构化跳过验证结果并继续到 W10。将 provider 改为
`openai_compatible` 并配置本地 DashScope 凭据后，W06/W07 会调用 `qwen-plus`。

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

## Demo Run：JSON + Markdown 单井报告（Task 006）

在仓库根目录执行；以下显式选用离线 Mock 和内存存储，无需模型密钥或数据库：

```bash
uv sync --locked --dev
CNLC_MODEL_PROVIDER=mock CNLC_PERSISTENCE=memory uv run cnlc-agent \
  --well-id WELL_MOCK_001 \
  --data-dir mock_data \
  --output-dir outputs
```

保留现有 CLI 和按任务分目录的规范，可重复运行而不覆盖之前的报告。
命令标准输出为 JSON，包含 `task_id`、`status`、`result`、`report` 路径：

```text
outputs/<task_id>/
├── result.json
└── report.md
```

JSON 保留 InterpretationState 契约，可再次解析；导出时隐藏凭据字段及原始错误消息，
保留稳定错误代码。Markdown 展示井信息、采样与曲线、QC、岩性、物性、流体、
油气水层分类、层段顶底深与厚度、综合验证和最终执行状态。
每阶段展示 `source`、`is_mock` 和 Demo Skip 标记；仅展示 State 已有结果，不计算专业结论。
`result.demo_skipped=true` 明确标为 Demo Skip；执行记录中的 `SKIPPED` 单独展示。
未生成结果不冒充跳过或成功。真实模型基于 Mock 输入的输出也不是经过验证的真实解释结论。

```bash
uv run pytest tests/integration/test_demo_report_e2e.py -v
```

该测试覆盖成功、WARNING、JSON 契约、Markdown 关键结果，以及模拟模型鉴权失败时
CLI、日志和报告不泄漏测试密钥或上游原始异常。全部离线，不消耗公网模型额度。

**集成状态（2026-09-21）：** Task 005 和 Report 已合入 `demo/2026-09-22`。
已验证 `CNLC_MODE=demo` 配合 Mock provider 的 W01–W10、W09 Demo Skip 及两份报告。
演示命令如下：

```bash
CNLC_MODE=demo CNLC_MODEL_PROVIDER=mock CNLC_PERSISTENCE=memory \
uv run cnlc-agent --well-id WELL_MOCK_001 --data-dir mock_data --output-dir outputs
```

**Integration Dependency：** qwen-plus 最终 E2E 仍需有效本地模型配置后运行同一命令，
将 `CNLC_MODEL_PROVIDER` 改为 `openai_compatible`；不要把 Mock E2E 或 Task 004
模型连通性测试当作真实模型最终 E2E 已通过。

## AgentScope 官方 Web UI（Task 007）

本仓库使用 AgentScope 2.0.8 Agent Service，官方前端快照位于
`frontend/agentscope-web`，上游来源见其中的 `UPSTREAM.md`。
Agent Service 使用 Redis 保存官方 UI 的 Agent、Session、消息和凭证配置；测井解释任务仍由
现有 `InterpretationTaskService` 按 `CNLC_PERSISTENCE` 使用内存或 PostgreSQL/Redis。

先启动 Redis，并在仓库根目录配置本地环境。模型统一选择 `qwen-plus`。
上传请求由接入层直接启动业务 Tool，W06/W07 使用现有 ModelGateway；最终报告直接返回，
不再让外层模型重新生成专业结论。模型配置放在本地 `.env` 或环境变量中：

```dotenv
REDIS_URL=redis://localhost:6379/0
CNLC_MODE=demo
CNLC_MODEL_PROVIDER=openai_compatible
CNLC_PERSISTENCE=memory
MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MODEL_NAME=qwen-plus
MODEL_API_KEY=在本地设置
```

如需让解释任务进入现有 PostgreSQL/Redis 持久化边界，将 `CNLC_PERSISTENCE` 改为
`postgres-redis` 并同时配置 `DATABASE_URL`。AgentScope 服务元数据始终使用 `REDIS_URL`。

安装锁定依赖并启动后端：

```bash
uv sync --locked --dev
uv run python -m cnlc_agent.demo.agentscope_app
```

服务默认监听 `http://localhost:8000`，并提供官方 Agent Service 的 `/health`、Agent、
Session、Chat 和 SSE 接口。另开终端启动 AgentScope 官方 Web UI：

```bash
cd frontend/agentscope-web
pnpm install --frozen-lockfile
pnpm dev
```

首次打开官方 Web UI 时：

1. Server URL 填写 `http://localhost:8000`，Username 可填写本地演示用户名。
2. 在 Credential 页面新增 DashScope 凭证；Key 只保存在本地 Agent Service 的 Redis 中。
3. 在 Chat 页面创建 Agent 和 Session，并为 Session 选择 DashScope 的 `qwen-plus`。后端会把
   该会话装配为 `LoggingInterpretationDemoAgent`；如果选择其他模型，会明确拒绝执行。
4. 点击输入框附件按钮，上传一份 `mock_data/WELL_MOCK_001.json`（或其他已有井资料
   JSON），输入 `帮我解释一下这口井` 并发送。无需填写 well_id、task_id 或 instruction。

页面通过官方 SSE 显示“读取井资料 / 检查数据”、W01–W10 实时开始与完成状态、
`run_well_interpretation` Tool Call/Result、“正在生成报告”和完整 Markdown 报告。
运行中可使用原有停止按钮。调用路径为：

```text
官方 Web UI → Agent Service → LoggingInterpretationDemoAgent / UploadInterpretationReply
→ 解析本次附件，读取或生成井标识
→ run_well_interpretation(well_id) → InterpretationTaskService
→ MainAgent → W01-W10 → Markdown Report
```

离线验证不访问模型公网，也不需要启动 Redis：

```bash
uv run pytest tests/integration/test_demo_agentscope_web.py \
  tests/integration/test_demo_web_upload.py tests/integration/test_demo_web_http.py -q
```

当前上传格式为现有 `MockFixture` JSON，包含 `well`、`raw_data`、`requirements`、
`outputs`、`validation`，最多一份、5 MiB；TXT 附件中包含同样 JSON 也可。
`well.well_id` 可省略，服务端自动生成；专业结果必须保留 `is_mock=true`。
上传内容在每次任务的独立临时目录中校验和执行，不覆盖仓库样例。
暂不支持原始 LAS/GDSX/CSV，也不会用另一口示例井的预设结论替代上传资料。

若只演示流程，设置 `CNLC_MODEL_PROVIDER=mock`；报告会明确标为 Demo / Mock。
已验证范围、真实模型联调和 Sol 待办见
[`008-demo-web-integration-handoff.md`](docs/tasks/008-demo-web-integration-handoff.md)。
