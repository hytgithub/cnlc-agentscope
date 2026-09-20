# Task 01：可运行工程骨架

- 状态：已实现并通过本任务验证。
- 执行日期：2026-09-20。
- 设计依据：`03-system-architecture.md` 第 76–77 节；同时遵循根目录 `AGENTS.md`。
- 起始版本：`54d75f5610b21fe74840ce637709b7944ef7f319`。
- 本任务验收对象：第一轮 End-to-End Skeleton，**不是 V0.1 完整 MVP**。

## 1. 本次实现

工程骨架按依赖顺序完成：

1. Python 3.11、uv、pyproject 与锁文件；配置、错误分类与开发工具。
2. 最小 Well / RawData / StageResult / ValidationResult / InterpretationState。
3. ModelGateway、WellRepository、TaskRepository、InterpretationStateStore、Telemetry 接口。
4. 显式 Mock 数据加载、工具、模型网关与内存演示适配器。
5. MainAgent、InterpretationAgent、ValidationAgent 三个 Python 调用骨架。
6. W01–W10 顺序、前置检查、结构化状态修改与终止分支。
7. JSON 序列化、Markdown 模板和命令行运行入口。
8. 单元测试、流程集成测试、工具契约/超时测试、结构化输出和 CLI 测试。

运行链路：

```text
CLI → TaskService → MainAgent → InterpretationWorkflow
W01 加载 → W02 完整性 → W03 Mock QC → W04 Mock 岩性 → W05 Mock 物性
→ W06/W07 InterpretationAgent → W08 Mock 层段 → W09 ValidationAgent
→ W10 结构检查 → JSON + Markdown
```

W06 通过统一 ToolCaller 调用 Mock calculate_sw。专业参数来自 fixture，未编写猜测公式。
Agent 只返回结构化结果，核心状态由 Workflow 写入。

## 2. 新增文件

| 文件/目录 | 内容 |
|---|---|
| `pyproject.toml`、`uv.lock`、`.python-version` | Python 3.11 与可复现依赖 |
| `.env.example`、`.gitignore` | 配置示例与敏感/生成文件排除 |
| `README.md` | 安装、运行、验证和边界说明 |
| `src/cnlc_agent/domain/` | 数据模型、状态、统一枚举和错误类型 |
| `src/cnlc_agent/config/` | AppSettings、未来连接配置占位 |
| `src/cnlc_agent/application/` | 接口、装配和任务服务 |
| `src/cnlc_agent/agents/` | 三个核心 Agent 骨架 |
| `src/cnlc_agent/workflows/` | 节点、顺序执行器和 W01–W10 处理器 |
| `src/cnlc_agent/tools/` | 工具契约、统一调用与 Mock 实现 |
| `src/cnlc_agent/infrastructure/` | Fixture Repository、MockModelGateway、内存适配器和日志事件 |
| `src/cnlc_agent/reports/` | State 转 JSON/Markdown |
| `src/cnlc_agent/main.py` | CLI 入口 |
| `mock_data/WELL_MOCK_001.json`、`mock_data/README.md` | 虚构数据与格式说明 |
| `tests/` | 单元和集成测试 |
| `docs/tasks/001-project-skeleton.md` | 当前任务的执行及验收记录 |

## 3. 修改文件

本次从只有文档的仓库新增工程文件；原有六份设计/规范文件未修改。
完整文件清单以本次 Git 提交为准。

## 4. 核心设计

- `domain` 仅依赖 Python/Pydantic，不引用 AgentScope、数据库或 Redis SDK。
- `application/bootstrap.py` 集中选择 Mock 实现；Workflow 中没有运行模式分支。
- Agent 收到独立上下文，节点收到 State 快照，通过 StatePatch 返回修改。
- State 保留 task_id、trace_id、步骤执行 ID、时间、状态、错误与修改前后记录。
- ToolCaller 统一超时和失败处理；失败不会生成专业成功结果。
- 内存 Repository/StateStore 使用深拷贝保存和返回快照，防止引用污染。
- Schema 使用 `extra=forbid` 检出拼写错误；Well/RawData 预留 extensions。
- JSON 与 Markdown 来自同一个 State；失败任务输出诊断摘要，不冒充最终报告。
- 井 ID 和文件路径检查阻止越界读取；密钥配置采用 SecretStr，运行事件不输出配置。

**Pending final well-data schema.** 数据契约版本为 `0.1-skeleton`。
样例中 Required/Recommended 列表仅描述演示数据，不是正式专业判断标准。

## 5. 当前 Tool Contract

所有 Tool 输入均为 `ToolInput`，包含 task_id、trace_id、well_id、step_id、parameters。
输出为 `ToolOutput`，包含 status、data、warnings、errors、metadata。
超时统一由 `CNLC_TOOL_TIMEOUT_SECONDS` 配置，默认 10 秒。

| Tool Name | 单一职责 | data 内容 | Mock 实现 |
|---|---|---|---|
| `get_well_data` | 读取井基础与曲线资料 | WellData | GetWellDataTool + MockWellRepository |
| `check_curve_quality` | 提供 QC 结果 | StageResult | MockResultTool/qc |
| `identify_lithology` | 提供岩性结果 | StageResult | MockResultTool/lithology |
| `evaluate_petrophysics` | 提供物性评价结果 | StageResult | MockResultTool/petrophysics |
| `calculate_sw` | 提供 Sw 工具结果 | StageResult | MockResultTool/sw |
| `merge_intervals` | 提供层段结果 | StageResult | MockResultTool/intervals |

错误示例：WELL_NOT_FOUND、INVALID_FIXTURE、MOCK_TOOL_RESULT_MISSING、TOOL_TIMEOUT、
TOOL_REPORTED_FAILURE。错误携带 code/message/retryable，Workflow 补充 step_id。
Tool 成功契约与调用记录在正常链路测试中验证，失败/超时另有专门测试。

## 6. 分支处理

| 场景 | 本轮行为 |
|---|---|
| 正常数据 | W01–W10 → SUCCESS |
| 缺少声明的 Required 曲线/有效采样 | W02 → BLOCKED，记录 affected_step=W03 |
| 缺少声明的 Recommended 资料 | 记录 WARNING，继续 |
| Tool / Mock Model 失败或模型响应结构错误 | 当前步骤 FAILED，后续节点不执行 |
| 验证轻微冲突 | WARNING，继续最终结构检查 |
| 严重冲突或证据不足 | W09 → REVIEW_REQUIRED，保存当前结果 |

自动 Retry/Rollback 尚未实现。当前只保留 retry_count/rollback_count 字段与错误可重试信息，
计数均为 0。严重冲突会停止等待复核，不模拟已经回退成功。
后续 Task 必须实现架构中的有限次重试、回退目标校验、下游结果失效和复核后的恢复。

## 7. 验证与结果

验证环境：Python 3.11.16，依赖见 `uv.lock`。

| 命令 | 结果 |
|---|---|
| `uv sync --locked --dev` | 成功 |
| `uv run pytest` | **22 passed** |
| `uv run ruff check .` | 通过 |
| `uv run ruff format --check .` | 通过 |
| `uv run mypy` | 29 个源码文件检查通过 |
| `uv build` | sdist 与 wheel 构建成功 |
| `uv run cnlc-agent --well-id WELL_MOCK_001` | SUCCESS；生成 result.json 和 report.md |

测试覆盖：正常链路与事件关联、JSON 往返、重复任务隔离、Required 缺失、Recommended 缺失、
Tool 失败、Model 失败、无效结构化响应、轻微/严重验证冲突、证据不足、未知井、曲线长度不符、
深度异常、超时、内存状态快照、路径越界、配置校验与 CLI 成功/失败退出码。

生成的输出、虚拟环境、构建产物和本地配置不进入仓库。

## 8. 未完成内容与 MVP 对应关系

| 能力 | 本任务状态 |
|---|---|
| W01–W10、State、工具契约、JSON/Markdown | 骨架可运行；业务语义与专业算法尚待实现 |
| 三个 Agent | Python 调用骨架；尚非真实 AgentScope Agent Runtime |
| 模型访问 | ModelGateway 接口 + Mock 响应回放；没有真实模型请求 |
| PostgreSQL / Redis | 接口与内存演示适配器；没有真实连接或持久化验收 |
| Trace | 可关联的 JSON 日志事件；尚非 OpenTelemetry Span/OTLP 导出 |
| 报告 | 模板渲染；尚无普通 LLM 报告生成 |
| Retry / Rollback / Resume | 待实现 |
| AgentScope Agent Service / 配套 Web UI | 待实现 |

因此 `mvp-acceptance.md` 的真实基础设施、模型、Runtime、Web 和回退验收项仍未完成，
不因本任务测试通过而标记为 MVP 完成。

## 9. Architecture Issue

**No Architecture Issue found.**

本次未改变核心 Agent 数量、业务顺序、数据库/Redis 技术选型和核心模块职责。
Mock 及接口占位来自总体架构第 76–77 节的第一轮范围；它们不会替代后续真实接入。

## 10. 下一阶段依赖

建议下一步先完成最小正式 Schema 与 Tool Contract 设计，再逐项接入 Runtime 和基础设施。
后续任务拆分需依据仓库中新增的 Task/设计说明，本记录不提前冻结尚未讨论的业务规则。

- 正式测试井样例、Required/Recommended 字段表、参数单位与缺失值约定。
- State/阶段结果的稳定 Schema、持久化表与 Redis 状态键/过期策略。
- 内部统一模型的协议、URL、模型名、鉴权方式和结构化输出能力；密钥仅本地配置。
- AgentScope 2.x 适配、Agent Context/Prompt、Tool 白名单。
- Retry/Rollback 次数与触发规则、状态恢复行为。

AgentScope 2.x 技术路线保持不变。已核对官方
[项目说明](https://github.com/agentscope-ai/agentscope)及
[发布信息](https://pypi.org/project/agentscope/)；Runtime 依赖将在实际接入任务锁定与验证，
本任务不引入未使用的大型框架依赖。
