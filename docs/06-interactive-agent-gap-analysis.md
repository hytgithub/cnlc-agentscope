# 交互式测井解释智能体：现状、差距与渐进迁移计划

> 分析基线：`codex/interactive-agent-2026-09-25`，`14c995c`；代码扫描与离线测试日期：2026-09-25。本文只分析现状和规划，不实现交互式功能。`Pending final well-data schema.`

## 1. Executive Summary

**结论：现有 Demo 适合渐进式升级。** W01～W10、统一 `InterpretationState`、标准 Tool Contract、模型访问层、报告归一化与模板、PostgreSQL/Redis 适配器、AgentScope Web/Chat/SSE 和离线 E2E 已存在。应保留 W01～W10 的业务顺序，在外层增加 Task/Execution 版本边界、四阶段聚合视图和受约束的任务命令；仅对 Workflow 入口做必要的受控续跑扩展。

当前最大限制不是缺少一个新的 Agent 类，而是：一次 `TaskRequest` 对应一次同步执行；`InterpretationState` 和 PostgreSQL 单行快照只保存该次运行；上传 Fixture 随调用结束删除；W03 只复制原始数据，没有重采样；专业 Mock 固定回放 Fixture；Web 上传 Middleware 每轮都要求文件，并且绕过外层 ReAct 推理。要验证“采样间隔/POR/PERM/模型切换确实改变新结果”，必须先补齐版本化输入、Override 和参数感知 Mock，不能只展示新的 Execution ID。

### 已具备（以代码为准）

1. 固定 W01～W10 的可执行 Demo，以及正常、缺失资料、Tool/模型失败、验证冲突等离线测试。
2. 六个内部注册业务 Tool（`get_well_data`、`check_curve_quality`、`identify_lithology`、`evaluate_petrophysics`、`calculate_sw`、`merge_intervals`）和一个 AgentScope 高层 Tool `run_well_interpretation`。
3. `MainAgent`、`InterpretationAgent`、`ValidationAgent` 三个业务类；W06/W07 走统一 `ModelGateway`，W09 在 Demo 模式显式跳过真实验证。
4. 可配置的 `MockModelGateway` / `OpenAICompatibleModelGateway`，具备 qwen-plus 的配置入口、JSON 校验和有限重试；本次未执行公网联调。
5. `ReportAssembler` → 归一化结果 → standard/compact Markdown，及脱敏 JSON；CLI 按唯一 `task_id` 输出两份文件。
6. SQLAlchemy 异步 PostgreSQL 单表、Alembic 初始迁移、Redis 运行时快照与 AgentScope 会话存储；默认业务持久化仍为内存。
7. AgentScope 官方 Web 前端快照、上传适配、W01～W10 进度、Tool Call/Result 卡片及基于现有 `/sessions/{id}/stream` 的 SSE。

### 文档与实现差异

| 文档期望 | 代码事实 | 迁移含义 |
|---|---|---|
| `docs/04`、`docs/05` 提出四阶段、六核心对象、异步执行和 SSE | 四阶段/Execution/Override/Artifact 仍是设计；但 W01～W10 的 `StepExecution`、Trace、Web SSE 已实现 | 不复制既有能力；四阶段先做聚合视图，事件传输复用现有协议 |
| `docs/01` 的 W03 包含深度对齐、重采样等候选处理 | `workflows/steps.py::qc` 调 Mock QC，并令 `processed_data=raw_data` | 采样间隔 0.1 必须补真实或明确受限的预处理契约；不能宣称已生效 |
| `docs/01` 的 W09 冲突后可 Rollback | 当前非 Demo 模式严重冲突进入 `REVIEW_REQUIRED`；Demo 模式 W09 返回 `demo_skipped=true` | 局部重跑与业务冲突回退是不同能力；后者仍待业务规则确认 |
| `docs/03` 规划 OTel Trace | `LoggingTelemetry` 是日志 span/event + 请求级观察器，尚无 OTLP 导出 | 可复用事件边界，不能将其称为完整分布式 Trace |
| `docs/04` 画出 Web → MainAgent ReAct → Task Tools | Web 实际进入 `LoggingInterpretationDemoAgent` 的上传 Middleware，直接调用唯一业务 Tool；内部 `MainAgent` 是 Python 编排类 | 交互 Agent 需要改入口路由，但保留三个业务类职责 |
| README 对 Web 链路简写为 `run_well_interpretation(well_id)` | 上传路径在 `RunWellInterpretationTool._call` 中调用 `run_uploaded_well` → `InterpretationToolRunner.run`；仅无注入 Runner 且无上传时直接调用该函数 | 两条路径共享 Service，不能误认为 CLI/Web 均直接调用同名函数 |
| `docs/05` 假定 DATA_DECODE 可复用上传文件 | `run_uploaded_well` 用 `TemporaryDirectory`，结束即删；Web 当前不保留输入 Artifact | 第一次重跑前必须保存可信输入版本或可恢复引用 |

## 2. 当前真实架构与完整调用链

### 2.1 `run_well_interpretation` 的准确位置和契约

`src/cnlc_agent/demo/tools.py::run_well_interpretation(well_id: str) -> dict[str, object]` 是 `async` 函数，调用模块级 `_default_runner.run(well_id)`，再 `DemoToolResult.model_dump(mode="json")`。它接收井号，返回 `status/task_id/well_id/completed_steps/step_statuses/steps/summary/report_markdown`；调用期间同步等待整条异步 Workflow 完成，**不会立即返回排队状态或后台 job**。`DemoToolResult` 中的状态来自最终 `InterpretationState.status` 和 `StepExecution`；非完成步骤被投影为 `PENDING`。

`task_id` 由 `TaskRequest` 的 UUID 默认工厂生成；`workflow_execution_id` 与 `trace_id` 也在新 `InterpretationState` 中生成。`workflow_execution_id` 只是一次运行标识，尚无可查询的 `Execution` 实体或历史版本关联。报告在 `InterpretationTaskService.run` 结束后由 `ReportAssembler.to_markdown(state)` 生成，并通过 `TaskRepository.save(state, markdown)` 按 `task_id` 存储。高层 Tool 只返回 Markdown 文本，不返回 CLI 的 JSON 文件路径。

实际调用链：

```text
Web 上传 JSON/TXT → AgentScope POST /chat/、会话 SSE
  → LoggingInterpretationDemoAgent
  → UploadInterpretationReply.on_reply（解析本轮附件；直接发 Tool 事件）
  → RunWellInterpretationTool.call(upload=fixture)
  → run_uploaded_well（临时 Fixture）→ InterpretationToolRunner.run
  → InterpretationTaskService.run → MainAgent.run → InterpretationWorkflow.run
  → W01…W10 → ReportAssembler → TaskRepository.save
  → DemoToolResult → AgentScope Tool Result / Markdown SSE / Web 卡片

Web 无附件、非注入 Runner 的高层 Tool 路径：
RunWellInterpretationTool._call → run_well_interpretation(well_id) → 同一 Runner/Service。

CLI `src/cnlc_agent/main.py::main` → application_runtime → 直接
InterpretationTaskService.run(TaskRequest) → 同一 MainAgent/Workflow/ReportAssembler
→ outputs/<task_id>/result.json + report.md。
```

CLI 不调用 `run_well_interpretation`。CLI 的 `--task-id` 是历史查询，不重新执行；输出目录 `exist_ok=False` 防覆盖，正常每次运行新 UUID，但没有同一持续 Task 下的版本关系。Web 展示的是 Tool Result 内报告；业务存储默认 `CNLC_PERSISTENCE=memory`，只有显式 `postgres-redis` 才持久化解释任务。

### 2.2 W01～W10 实际实现和四阶段映射

十个处理器均在 `src/cnlc_agent/workflows/steps.py::build_steps`，由 `src/cnlc_agent/workflows/interpretation_workflow.py::InterpretationWorkflow.run` 固定按 `StepId` 顺序执行。`WorkflowNode` 前置字段检查、每步前后状态快照、`StatePatch` 合并、异常转 `FAILED`、遇 `BLOCKED/FAILED/REVIEW_REQUIRED` 停止，都已实现；业务 Retry/Rollback 尚未实现。表中输入为实际读取的主要字段。

| Step | 代码位置/当前能力 | 执行主体 | 主要输入 → 输出 | Tool / Agent / Workflow；Mock情况 | 目标阶段/处理 |
|---|---|---|---|---|---|
| W01 | `steps.py::load`；加载 Fixture | Workflow + `ToolCaller` | `TaskRequest.well_id` → `well/raw_data/data_requirements` | `get_well_data`，文件读取是真执行、数据为 Mock Fixture | DATA_DECODE；KEEP + 输入持久化 WRAP |
| W02 | `steps.py::completeness`；完整性规则 | Workflow 内部 | `well/raw_data/data_requirements` → `missing_data/warnings` | 非 Tool；Demo 模式直接放行，非 Demo 做最小检查 | PREPROCESS；KEEP，复用时必须携带校验结果 |
| W03 | `steps.py::qc`；QC | Workflow + Tool | `raw_data` → `qc_result/processed_data` | `check_curve_quality` 回放 Mock；`processed_data=raw_data`，无重采样 | PREPROCESS；局部 REFACTOR |
| W04 | `steps.py::lithology`；岩性 | Workflow + Tool | `processed_data/qc_result` → `lithology_result` | `identify_lithology` 回放 Mock | INTERPRET；KEEP/WRAP |
| W05 | `steps.py::petrophysics`；物性 | Workflow + Tool | `lithology_result/qc_result` → `petrophysics_result` | `evaluate_petrophysics` 回放 Mock，未独立计算孔隙度/渗透率 | INTERPRET；KEEP/WRAP |
| W06 | `steps.py::fluid`；流体 | `InterpretationAgent` + Tool + LLM | 前序解释上下文 → `fluid_result`（含 `sw_result`） | 先 `calculate_sw` Mock，再 `ModelGateway.generate(purpose=fluid)`；模型可为 Mock 或 qwen-plus | INTERPRET；KEEP/WRAP |
| W07 | `steps.py::classification`；层分类 | `InterpretationAgent` + LLM | `fluid_result/petrophysics_result` → `layer_classification` | `ModelGateway.generate(purpose=classification)`；Mock 或 qwen-plus | INTERPRET；KEEP/WRAP |
| W08 | `steps.py::intervals`；层段 | Workflow + Tool | `layer_classification/petrophysics_result` → `interval_result` | `merge_intervals` 回放 Mock，非实时厚度计算 | INTERPRET；KEEP/WRAP |
| W09 | `steps.py::validate`；验证 | `ValidationAgent` 或 Demo 占位 | `layer_classification/interval_result` → `validation_result` | Demo 返回显式 `demo_skipped=true` 的 SUCCESS 占位；非 Demo 走模型，严重冲突转 REVIEW_REQUIRED | INTERPRET；KEEP，保留跳过标记 |
| W10 | `steps.py::final_check`；结构检查 | Workflow 内部 | W03～W09 结果及错误 → `final_check` | 不调用 Tool/LLM；`is_mock=true` 的 skeleton 检查 | INTERPRET 收尾；KEEP；REPORT 是其后的现有报告链 |

**映射结论：可以保留 W01～W10 作为四阶段内部步骤，且应优先如此。** 四阶段是 Execution 展示和失效范围，不是替换十步业务流程。现有 W10 名称包含“报告生成”，但代码中的 W10 只做结构检查；真正 Markdown/JSON 在 Workflow 返回后的 Service/CLI 中生成。因此 REPORT 应是独立的外层阶段，不能把 W10 的完成误当报告已完成。W02～W03 可聚合为 PREPROCESS，W04～W10 可聚合为 INTERPRET；必要时保留 W10 的“最终检查”作为 INTERPRET 的最后门槛。

进入报告的结果：`ReportGenerator` 将 State 中井信息、曲线/QC、岩性、物性、流体、层分类、层段、验证、建议/局限等归一化；失败状态生成诊断摘要。W01/W02 的原始数据与缺失信息也参与报告事实投影；`executions/changes` 主要进入 JSON 和 Web 步骤视图，而非完整 Markdown 过程清单。

## 3. Agent / Tool / State / Persistence / Model / Report / Web 现状

### 3.1 所有已注册业务 Tool

内部 Tool 注册列表来自 `src/cnlc_agent/application/bootstrap.py::build_application`；Web 注册列表来自 `src/cnlc_agent/demo/agentscope_app.py::demo_agent_tools`。除下表七项，未发现项目另行注册的业务 Tool。`ToolInput` 统一含 `task_id/trace_id/well_id/step_id/parameters`，`ToolOutput` 统一含 `status/data/warnings/errors/metadata`。内层 Tool 调用都经 `ToolCaller` 超时、校验、错误归类与 Trace；但它不保存结构化 ToolRun。

| Tool | 代码位置 | 当前输入 → 输出 | Mock/Real | 谁调用 | 可复用性 |
|---|---|---|---|---|---|
| `get_well_data` | `tools/mock.py::GetWellDataTool` | `ToolInput.well_id` → `WellData` 三字段 | 真实文件 I/O + Mock 资料 | W01 | 高；保留 Contract，换 Repository 可变 REAL |
| `check_curve_quality` | `tools/mock.py::MockResultTool(qc)` | `well_id` → Fixture `StageResult` | MOCK | W03 | 高；需另补预处理/重采样，不能将此 Tool 冒充重采样 |
| `identify_lithology` | 同上，`lithology` | `well_id` → `StageResult` | MOCK | W04 | 高；未来可作 VIRTUAL 投影 |
| `evaluate_petrophysics` | 同上，`petrophysics` | `well_id` → `StageResult` | MOCK | W05 | 高；未来可作 VIRTUAL 或替换真实算法 |
| `calculate_sw` | 同上，`sw` | `well_id` → `StageResult` | MOCK | W06 的 InterpretationAgent | 高；若有正式公式应优先 REAL，不能让 LLM 估算 |
| `merge_intervals` | 同上，`intervals` | `well_id` → `StageResult` | MOCK | W08 | 高；未来可用确定性 REAL 算法 |
| `run_well_interpretation` | `demo/tools.py::RunWellInterpretationTool` | AgentScope `well_id` → 整个 Demo DTO | 真实编排入口；专业结果可能 Mock | Web/Middleware | 保留为兼容入口，外层增加任务命令；不将它拆为六个专业 Agent Tool |

现有 `ToolInput.parameters` 和 `ToolOutput.metadata` 可扩展传入/返回 execution、来源信息；更合适的做法是在 `ToolCaller` 外围记录 `ToolRun`，从调用边界采集真实输入摘要、输出摘要、状态和耗时。`execution_mode/source` 应由物理 Provider/Tool 明确给出并校验，不能仅按 Tool 名称或 UI 的 `_source` 推断。ToolRun 失败也要落记录。目前 `demo/presentation.py::_source` 对 W06/W07 硬编码 `qwen-plus`，即便使用 MockModelGateway；迁移时需改为实际调用来源。

### 3.2 Agent 体系与边界

| 类 | 实际职责与框架关系 | 当前上下文/Tool | 迁移判断 |
|---|---|---|---|
| `agents/main_agent.py::MainAgent` | 普通 Python 编排类，启动 `InterpretationWorkflow.run`；没有 AgentScope ReAct 循环或任务级 Tool | `TaskRequest` + `InterpretationState`；无模型/直接专业 Tool | 保留 Planner/Orchestrator 业务角色；交互入口在外层加受约束路由/命令 |
| `agents/interpretation_agent.py::InterpretationAgent` | W06/W07 结构化推理 | W06 经 `ToolCaller` 调 `calculate_sw` 后调用 `ModelGateway`；W07 直接调用 Gateway | 保留；Override/模型结果通过明确上下文注入，不改为 LLM 专业计算 |
| `agents/validation_agent.py::ValidationAgent` | W09 结构化验证 | 调 `ModelGateway`；Demo 模式由 Workflow 跳过 | 保留；不直接覆盖解释结果 |
| `demo/demo_agent.py::LoggingInterpretationDemoAgent` | 真正继承 AgentScope `Agent`，配置 ReAct/Toolkit，但上传 `UploadInterpretationReply` 代替默认 reply 路径 | Toolkit 仅注册一个高层 `run_well_interpretation`；会话 `AgentState.context` 保存消息 | 适合作交互外壳的改造点；目前**上传路径没有经 ReAct 模型识别修改/查询意图** |

Web 的 AgentScope 聊天上下文和 Redis 会话消息可复用作短期会话，但真实 `task_id/current_execution_id` 必须由可查询的业务 Task 映射，不应以聊天文本作为唯一事实。Agent 与 Workflow 的业务边界总体清晰；待改的是 Web 入口将每轮消息都解释成一次带附件的完整执行，以及 Task 命令尚不存在。`MODEL_NAME=qwen-plus` 是 Web 外壳的模型限制，不等于“模型 B”的预测模型配置；后者是未来 PredictionGateway 的另一个维度。

### 3.3 状态与六个目标对象

| 目标对象 | 最接近的现有对象/事实 | 建议最小迁移 |
|---|---|---|
| `InterpretationTask` | `TaskRequest` 含一次请求 ID；`TaskRow` 为 `interpretation_task` 单行；`InterpretationState` 是单次运行的聚合快照 | **扩展现有 TaskRow/TaskRepository 的持续任务语义**，保留 TaskRequest 作为单次命令输入；不再把 TaskRow.snapshot 当全部历史 |
| `Execution` | `InterpretationState.workflow_execution_id` 只有 ID，`StepExecution` 名字易混淆 | 新增最小 `execution` 实体/表，存 task_id、序号、触发原因、配置/Override/计划快照、状态和本次 State 快照；每次运行新建，历史只读 |
| `StageRun` | `StepExecution`（W01～W10）与 `completed_steps/current_step` | **保留 StepExecution**，先按固定映射聚合四阶段读模型；后续为持久阶段状态/REUSED 引用新增轻量 StageRun 记录，不复制十步结果 |
| `ToolRun` | `ToolCaller` Span/`tool.result` 事件；AgentScope 高层 Tool Call 消息 | 增加结构化持久记录，可由 `ToolCaller` 包装产生；日志和 AgentScope 消息只能部分承担，无法可靠查询内层 Tool 输入/输出、失败及物理来源 |
| `InterpretationOverride` | `TaskRequest.instruction` 是自由文本；`ToolInput.parameters` 可传参数但目前 Mock 不读取；无采样间隔/预测模型正式字段 | 独立、严格校验的每 Execution Override 快照，放应用命令/规划层并传至 Workflow 上下文；原始 `RawData` 与 Fixture 不改写 |
| `Artifact` | `mock_data/*.json`、Web 临时上传、State、CLI `outputs/<task_id>/result.json/report.md`、数据库 `markdown` | 一期先用**最小文件引用/元数据**（输入内容摘要、受控 URI、来源 Execution、类型），不必立刻建通用 Artifact 大表；但上传来源必须持久留存，报告路径必须绑定 Execution |

`InterpretationState` 是 Pydantic 可变模型，`StatePatch` 合并时会重建并验证 State，节点接收深拷贝，但它**不是不可变 Execution 快照**。`StateChange` 记录 actor、步骤、时间、前后差异；状态列表 `executions` 存 W01～W10 单步尝试，且 `workflow_execution_id` 每次新 State 生成。当前不能一个 Task 对应多次运行；新任务总是生成新 `task_id`，同一 ID 在仓库中被拒绝，若将旧 State 直接重跑会触发 `TASK_ALREADY_STARTED`。当前 `TaskRow.snapshot` 和 `markdown` 按 task_id 原位更新，重跑若复用 task_id 会覆盖旧内容；CLI 新 UUID 只是避免文件覆盖，不是版本控制。

### 3.4 Persistence 与后台状态

`application/ports.py::TaskRepository` 定义 create/get/save/get_report；`infrastructure/database.py::PostgreSQLTaskRepository` 与 `infrastructure/mock.py::InMemoryTaskRepository` 实现。`migrations/versions/0001_task_snapshots.py` 只有 `interpretation_task` 一张表，字段为 `task_id/well_id/status/snapshot JSONB/markdown/created_at/updated_at`。StepExecution 和 StateChange **作为 snapshot JSONB 内容**持久化，非单独表；ToolRun 无持久化。`application/checkpoints.py::CheckpointStore` 每步先更新 PostgreSQL 同一快照及空 markdown，再写 Redis；末尾 Service 将最终 Markdown 写回同一行。增加 Execution/StageRun/ToolRun 需要新增迁移和 Repository 方法，但能沿用现有连接、事务和 JSONB 模式，不需替换数据库架构；大体量原始曲线不宜无限复制到每个 Execution JSONB。

`RedisInterpretationStateStore` 用 TTL 保存运行快照；AgentScope `RedisStorage` 保存 Agent/Session/消息/凭证。两者不能混为一个业务任务队列。当前无后台 Worker、业务 job 队列、租约/锁、自动恢复或持久事件流。`UploadInterpretationReply` 中的 `asyncio.create_task` 仅并发处理当前 HTTP 回复与进度队列；取消回复时会取消 Tool，不能承担跨请求长任务。查询真实状态可在 PostgreSQL 模式读取 `TaskRepository.get`，但还没有 Agent 可调用的 `get_task_status`，默认内存服务每次创建即关闭也无法跨请求查询。

### 3.5 ModelGateway 与报告

`application/ports.py::ModelGateway` 的 `generate(ModelRequest)->JsonObject` 已有 `MockModelGateway` 与 `OpenAICompatibleModelGateway`；`bootstrap.py` 按配置注入。真实网关负责异步 HTTP、qwen-plus 配置、JSON object 校验、错误映射及传输重试。**KEEP**：交互意图识别若需要模型，可复用此统一访问层，但需单独设计目的/Schema 和上下文，不能让不同 Agent 自建 SDK 客户端。预测模型 A/B/C 的 Heavy API 不是这个 qwen-plus Gateway；未来另设 `PredictionProvider` 接口即可，勿重构已验收 ModelGateway。

`ReportAssembler` 读取本次 `InterpretationState`：完成态经 `ReportGenerator → normalize_interpretation_result → standard/compact renderer`，失败态生诊断摘要；`to_json` 输出脱敏 State。Service 按 task_id 存 Markdown；CLI 按 task_id 建互斥输出目录。当前新 Task 不覆盖旧文件，但同一 Task 内没有 execution_id，也没有报告版本查询。报告模块判为 **WRAP**：保留事实归一化与模板，外层增加 `(task_id, execution_id)` 的报告引用和命名空间；不要求重写模板。Web 当前仅收到 Tool Result 的 Markdown，不见 CLI 本地 JSON 路径。

### 3.6 Web UI 的真实能力

浏览器 `frontend/agentscope-web` 发 `/chat/` 请求并订阅 `/sessions/{session_id}/stream`；附件经 `ChatViewport.tsx` 转为 Base64 DataBlock/TXT TextBlock，`demo/uploads.py::parse_upload` 校验至多一份、5 MiB、MockFixture 或显式合成上下文，当前不接受原始 LAS/GDSX/CSV。`UploadInterpretationReply` 在回复期间把 Telemetry 事件压成 W01～W10 文本进度，发 AgentScope Tool Call/Result 与分块 Markdown；`RunWellInterpretationRenderer.tsx` 在完成后展示十步卡片。**已有流式响应，也展示一个高层 Tool 调用和步骤状态**；没有持久四阶段/内层 Tool 状态、历史 Execution 选择或跨会话状态查询。当前每轮无附件会返回“请上传”，自然语言改参数和状态查询均不能进入任务命令。

一期可复用 AgentScope SSE 和现有前端卡片体系：先把后端 Execution/Stage/Tool 状态投影为官方消息/事件或按 ID 查询；若后台作业需断线重连与事件回放，再评估增量事件 API/SSE。**无需先新建第二条 SSE 通道，更不需重做前端**；仅用当前请求级 `ContextVar` 进度流也不足以支撑后台长任务和真实状态查询。

## 4. KEEP / WRAP / REFACTOR / ADD / DEPRECATE 决策

| 模块 | 当前职责 | 目标职责 | 处理方式 | 理由 | 风险 |
|---|---|---|---|---|---|
| `run_well_interpretation` | 同步等待一次完整 Demo 并返回 DTO | 兼容首次解释入口 | WRAP | 保留现有调用与测试，在外层提供任务命令 | 旧返回值无 execution_id |
| W01 | 读取 Fixture | DATA_DECODE 内部步骤 | WRAP | 可保持 Tool/Step | 上传临时文件丢失 |
| W02 | 完整性规则/Demo 放行 | PREPROCESS 内部步骤 | KEEP | 规则边界明确 | Demo 放行不能宣称真实 QC |
| W03 | Mock QC + 原样复制 RawData | PREPROCESS/QC/重采样 | REFACTOR | 采样间隔需真实进入输入版本 | 无确定的正式重采样规则 |
| W04 | Mock 岩性 | INTERPRET 子步骤 | WRAP | 现有 Contract 可投影 | 固定 Fixture 输出 |
| W05 | Mock 物性 | INTERPRET 子步骤 | WRAP | 可接 Override/Provider | 固定结果不响应 POR/PERM |
| W06 | Sw Tool + 模型流体识别 | INTERPRET 子步骤 | WRAP | 保留解释 Agent | Mock Sw 与新 Override 来源需核对 |
| W07 | 模型层分类 | INTERPRET 子步骤 | WRAP | 保留解释 Agent | 模型输入版本需追踪 |
| W08 | Mock 层段 | INTERPRET 子步骤 | WRAP | 保留 Tool Contract | 厚度未独立计算 |
| W09 | 验证或 Demo Skip | INTERPRET 子步骤 | KEEP | 结构化冲突与跳过已明确 | Demo Skip 不等于验证通过 |
| W10 | 结构最终检查 | INTERPRET 收尾 | KEEP | 不改变 W01～W10 顺序 | 报告实际在 Workflow 外 |
| `InterpretationWorkflow` / `WorkflowNode` | 固定从 W01 运行 | 按受控 ExecutionPlan 起点运行 | REFACTOR | 最小改入口/复用校验，不重写处理器 | W10 检查完整 `completed_steps`，复用标记需适配 |
| `InterpretationState` / `StepExecution` | 单次任务状态/十步记录 | 每次 Execution 的 State、十步记录 | WRAP | 维持唯一业务状态和审计边界 | 可变对象需冻结历史快照 |
| `MainAgent` | 启动固定 Workflow | 业务级编排 | WRAP | 保持既有职责 | 外层交互类同名混淆 |
| `InterpretationAgent` | W06/W07 | 继续结构化解释 | KEEP | 不删除核心 Agent | 新参数必须进入显式上下文 |
| `ValidationAgent` | W09 | 继续独立验证 | KEEP | 不覆盖解释结果 | Demo Skip 范围 |
| 六个内部 Tool | 井读取/五项 Mock 结果 | 按执行模式复用或替换 | WRAP | `ToolInput/Output` 正式契约已在 | Mock 结果无参数感知 |
| `ToolCaller` | 超时/校验/Trace | ToolRun 记录边界 | WRAP | 集中采集调用轨迹 | 不可把日志当持久 ToolRun |
| AgentScope 高层 Tool / Demo Agent | 上传后一次完整解释 | 兼容入口与受约束任务命令 | REFACTOR | Web 已有 AgentScope 基础 | Middleware 绕开意图识别 |
| `ModelGateway` 两实现 | qwen-plus/Mock JSON 模型调用 | 交互理解及 W06/W07 的统一入口 | KEEP | 已有独立验收与错误处理 | 与预测 Heavy API 混淆 |
| `ReportAssembler`/renderers | State → JSON/Markdown | Execution 版本化报告 | WRAP | 事实投影和模板可沿用 | 报告绑定目前仅 task_id |
| `TaskRepository` / PostgreSQL / migration | 单行任务快照与报告 | 持续 Task + 多 Execution | REFACTOR | 增量表/接口即可 | 并发、原位覆盖及大 JSONB |
| Redis StateStore / AgentScope RedisStorage | TTL 快照/聊天会话 | 继续缓存/会话，必要时事件或队列 | WRAP | 已接入真实 Redis | 默认内存模式跨请求不可见 |
| CLI | 首次执行/按 task_id 查历史 | 保持回归和兼容；后续可按 Execution 查 | WRAP | 已验收 E2E | 旧输出目录不能承载多版本 |
| Web UI/现有 SSE | 上传、进度、十步卡片、报告 | 增量展示版本/阶段/工具及命令 | WRAP | 官方流式机制存在 | 后台断线状态需独立查询 |
| 现有测试 | 合同、Workflow、报告、Web/HTTP/CLI | 旧链路回归基线 | KEEP | 92 项当前通过 | Mock 通过不代表真实联调 |
| 四阶段读模型、Execution/Override/计划/任务命令 | 不存在 | 交互执行骨架 | ADD | 当前无等价实体 | 顺序、原子性与复用来源 |
| 独立 Artifact 大表、第二套 SSE | 尚不存在 | 必要时再引入 | DEPRECATE（对一期提案） | 先用最小引用与官方流式协议；当前无待删除实现 | 若外部 API/存储需求扩大需再评估 |

## 5. 目标能力 Gap（EXISTS / PARTIAL / MISSING）

| 能力 | 状态 | 证据与确切缺口 |
|---|---|---|
| 持续交互 Task | PARTIAL | 有 task_id、TaskRow、会话；一次请求即一次任务，无当前 Execution 指针或多轮绑定 |
| Execution Version | PARTIAL | 有 `workflow_execution_id` 和 StepExecution；无 Execution 实体、序号、历史版本 |
| User Override | MISSING | 只有自由文本 `instruction`/未使用的 Tool 参数；无结构化快照 |
| ExecutionPlan | MISSING | 固定从 W01 遍历，无 RUN/REUSE 计划 |
| DependencyResolver | MISSING | 无字段影响范围表/代码 |
| 局部重跑 | MISSING | Workflow 拒绝非 PENDING，W10 要完整 completed_steps |
| Task-level Agent Tools | PARTIAL | 有高层 `run_well_interpretation`；缺修改、全重跑、状态、报告命令 |
| 异步长任务 | PARTIAL | Python async 和 Web 请求期 create_task 存在；无可脱离请求的队列/worker/恢复 |
| StageRun | PARTIAL | 可按 StepExecution 聚合四阶段；无版本化阶段持久状态和 REUSED 来源 |
| ToolRun | PARTIAL | ToolCaller Trace 与 AgentScope Tool 消息；无内层工具持久行及执行模式 |
| Virtual Tool Projection | MISSING | 专业 Mock 各自回放；无 Heavy API 结果归一化/投影 |
| Execution-report 绑定 | PARTIAL | task_id ↔ Markdown/CLI 文件已有；无 execution_id ↔ 报告 |
| 实时 Stage/Tool Event | PARTIAL | W01～W10 文本进度和官方 SSE 已有；无持久 Stage/内层 Tool 事件及断线回放 |

## 6. 局部重跑：最小侵入方案

当前 `InterpretationWorkflow.__init__` 校验必须有完整 W01～W10；`run` 强制 State 为 PENDING 并从 `self.nodes` 首项开始。`StatePatch` 的业务结果字段与 `StepExecution` 记录为复用提供基础，但没有能直接从 W03/W04 启动的公开接口。**不能只过滤 `nodes`：** W10 比较 `completed_steps == [W01…W09]`，报告读取所有前序字段，且旧执行日志、错误/告警和结果必须明确标记来源。第一版采用外层 `ExecutionPlan` + 新 Execution 的 State 快照，再小范围扩展 Workflow 入口：

1. 从已完成且输入指纹匹配的旧 Execution 复制**允许复用的业务结果字段**，不复制旧执行记录/未解决错误；对 W01/W02、必要时 W03 写 `REUSED` StageRun 和 `source_execution_id`。`StepStatus` 不盲目加与已有八态重复的全局状态；`REUSED` 先作为执行计划/StageRun 动作语义。
2. `DependencyResolver` 只接受白名单字段，确定最早失效阶段与下游 RUN；`FULL_RERUN` 全 RUN。未知字段拒绝或转人工确认，不能静默复用。
3. Workflow 提供受控 `start_step`/`reuse_manifest`（或等价 Adapter 参数），保留现有 `WorkflowNode` 和 W01～W10 顺序；在进入起点前校验复用快照含所需前置字段。`completed_steps` 对当前有效链路既含本次成功步骤也含经验证的复用步骤，`StepExecution` 仅记录本次真实运行；最终检查据有效完成清单工作。旧执行仍为只读。
4. 参数变化必须进入新的 Execution `config_snapshot/override_snapshot` 和本次 Tool/Model 输入；报告在本次 Execution 下生成。失败或取消不得更新 Task 的“最新成功报告”指针。

| 用户变化 | 计划阶段 | 现有阻点/最小补充 |
|---|---|---|
| `sampling_interval=0.1` | DATA_DECODE REUSE；PREPROCESS/INTERPRET/REPORT RUN | W03 目前无重采样；须定义明确输入版本与可验证的采样行为。若仅 Mock，可证明参数传递和输入版本变更，但必须明确未进行真实曲线重采样 |
| `POR=0.16, PERM=0.16` | DATA_DECODE/PREPROCESS REUSE；INTERPRET/REPORT RUN | 不改 RawData；Override 进入参数感知 Mock/后续 Heavy API 的文件输入，不能只改报告文案 |
| `prediction_model=B` | DATA_DECODE/PREPROCESS REUSE；INTERPRET/REPORT RUN | 当前 `model_provider` 是 qwen-plus/Mock LLM 选择，非预测模型；需独立 prediction_model 字段与 Mock 分支 |
| `FULL_RERUN` | 四阶段全部 RUN | 新 Execution 和独立报告；保留历史版本，复用输入 Artifact 但不得复用阶段结果 |
| 查询状态 | 不创建 Execution | 从业务 Repository 查询 task/current_execution/stage/tool；不能据 AgentState 消息推断 |

## 7. Heavy API / Logical Tool 渐进方案

一期交互闭环可继续用现有六个 Tool Contract 和 Mock Fixture，但 W03/解释 Mock 需**参数、模型、Execution 感知**，且每个结果写明 `is_mock/source/effective_parameters`。不要将固定回放误报为参数重算。`get_well_data` 的资料读取可先保持 Fixture，输入持久化后可接真实 Repository；预处理中的采样/单位/深度对齐更适合未来 REAL 的确定性 Tool/算法；`calculate_sw`、`merge_intervals` 在公式/规则确认后更适合 REAL。W04 岩性、W05 综合物性、W06/W07 流体/层分类，以及若外部综合结果包含层段的 W08，适合先 MOCK、接 Heavy API 后 VIRTUAL 投影；ValidationAgent 独立证据检查不应被预测 API 的同源输出冒充。

后续 INTERPRET 阶段新增独立 `PredictionProvider`/Gateway：`InterpretationInputVersion + Override + model_id` → 一份受控输入文件 → **一次**第三方 Heavy API → 原始返回 Artifact → `PredictionResultParser` → `NormalizedInterpretationResult` → 多个 `LogicalToolProjector` 结果。VIRTUAL 仅是**结果与 ToolRun 的投影**，不需要把它注册成 AgentScope 可自行调用的专业 Tool；每条 ToolRun 必须指向同一 `source_external_call_id`，真实调用只记录一次。文件协议、POR/PERM 写入方式、三模型选择、返回字段、报告 API 协议仍待外部确认；不能写入猜测的专业阈值或公式。`reports/models.py::NormalizedInterpretationResult` 是**报告事实 DTO**，不要误当 Heavy API 的归一化预测结果，可用不同命名/Adapter 边界区分。

## 8. 建议实施 Task 01～11

下列任务按单独提交、单独验收组织；每个任务完成均需保留 `uv run pytest -q` 的旧 E2E 回归，命令中的定向测试为该任务新增/扩展的验收范围。目录是建议影响范围，不代表本次已修改。

| Task | 名称、目标与现状依据 | 新增 / 修改 / 明确不修改 | 依赖；涉及文件/目录 | 验收标准；测试命令 | 风险；建议模型 |
|---|---|---|---|---|---|
| 01 | **持续 Task 与版本化 Execution 基座**。当前 TaskRow 单行覆盖、workflow_execution_id 只有 ID。保留 `InterpretationState` 作为每次运行的状态。 | 新增 Execution 契约/仓库方法/增量迁移；扩展 TaskRow 的 current/latest 指针与查询。先不改 Workflow、Tool、Agent、Web、ModelGateway、既有 CLI 语义。 | 无；`domain/`、`application/ports.py`、`infrastructure/database.py`、`infrastructure/mock.py`、`migrations/`、`tests/unit/test_persistence.py` | 同一 Task 可创建多条不可变 Execution，旧快照/报告不被覆盖，重复 ID/并发冲突明确失败；`uv run pytest tests/unit/test_persistence.py tests/unit/test_migrations.py tests/integration/test_real_persistence.py -q`（真实服务按 opt-in），再全量 pytest。 | JSONB 复制、事务原子性；**gpt-6-sol high** |
| 02 | **输入版本与 Override 契约**。Web 上传后临时目录删除，参数仅留在自由文本。 | 新增受控输入 Artifact 引用/摘要、Override 校验/快照、config snapshot；修改上传交接与应用层命令输入。暂不改 W01～W10 顺序、LLM、报告模板；不建通用 Artifact 大表。 | 01；`demo/uploads.py`、`demo/tools.py`、`domain/`、`application/`、`tests/integration/test_demo_web_upload.py` | 同井不同上传不串数据，旧输入可恢复；POR/PERM 不修改 RawData，非法值明确拒绝；`uv run pytest tests/unit/test_contracts.py tests/integration/test_demo_web_upload.py -q`。 | 上传隐私/存储寿命、正式井数据 Schema 未定；**gpt-6-sol high** |
| 03 | **确定性 ExecutionPlan**。当前流程固定从 W01 开始。 | 新增 DependencyResolver/ExecutionPlan 和指纹/复用校验；仅改应用服务调用接口，不改 Workflow 处理器、Agent、Web。 | 01–02；`application/` 或 `workflows/` 新规划模块、`tests/unit/` | 四种指定变化精确产出 RUN/REUSE；未知变更不复用；`uv run pytest tests/unit/test_execution_plan.py -q`。 | 参数影响规则漂移；**gpt-6-sol high** |
| 04 | **受控局部重跑 Adapter**。现有 Workflow 强制 PENDING/W01 起点，W10 需完整 completed_steps。 | 新增 State 复用装配/来源校验/StageRun 读模型；小范围修改 `InterpretationWorkflow.run` 入口和 W10 有效完成判断。保持 W01～W10 顺序与专业 Agent/Tool 职责，不重写节点。 | 03；`workflows/interpretation_workflow.py`、`workflows/steps.py`（仅必要处）、`domain/state.py`、`application/`、`tests/integration/test_workflow.py` | 采样从 W02/W03 重跑、POR/PERM/模型从 W04 重跑、全量 W01 起跑；复用来源可查，缺失/失败旧结果不能复用；`uv run pytest tests/integration/test_workflow.py -q`。 | 把旧执行错误/记录带入新状态；**gpt-6-astra high** |
| 05 | **参数感知 Mock 与预处理可验证性**。当前 W03 原样复制，专业结果固定 Fixture。 | 新增明确的预处理输入版本与 Mock Prediction Provider/Adapter，修改 Mock Tool/上下文以读取 effective parameters/model/execution；如正式重采样规则未确认，仅做清楚标注的 Demo 行为。保留 qwen-plus ModelGateway，勿补造物性公式。 | 02–04；`tools/mock.py`、`workflows/steps.py`、`domain/`、`tests/integration/test_workflow.py` | 改采样间隔后输入版本不同；POR/PERM 与模型 B 进入本次计算/Mock 输出与来源，旧数据不变；`uv run pytest tests/integration/test_workflow.py tests/unit/test_contracts.py -q`。 | Mock“参数感知”与真实计算混淆；**gpt-6-astra high** |
| 06 | **Execution 报告绑定与 ToolRun 记录**。现有报告/Trace 只按 task_id 或日志关联。 | 新增 execution_id 报告引用与 ToolCaller 结构化调用记录，修改 ReportAssembler 外层存储/CLI 可选历史查询；保留报告归一化、模板与 Tool Contract。 | 01、04–05；`application/ports.py`、`tools/contracts.py`、`reports/` 外层、`main.py`、`infrastructure/`、测试 | 两次 Execution 两份可查询报告，Mock/失败 ToolRun 有状态/source，不泄漏全井曲线/密钥；`uv run pytest tests/unit/test_reports.py tests/integration/test_demo_report_e2e.py tests/integration/test_cli.py -q`。 | 存储体积、敏感数据；**gpt-6-sol high** |
| 07 | **任务级命令与受约束交互入口**。当前上传 Middleware 每轮仅接受附件并启动完整解释。 | 新增 start/modify/full/status/get_report 命令 Tool 与 Intent/参数结构化边界，修改 `LoggingInterpretationDemoAgent`/Middleware 路由；保留内部 Main/Interpretation/Validation Agent 与 `run_well_interpretation` 兼容入口。 | 01–06；`demo/`、`application/`、`agents/`（仅外层适配）、`tests/integration/test_demo_agentscope_web.py` | 六句核心用户话术映射到正确命令；状态查 Repository 不发新 Execution；无附件的后续轮可用；`uv run pytest tests/integration/test_demo_agentscope_web.py tests/integration/test_demo_web_upload.py -q`。 | LLM 误提取、会话 task_id 与授权边界；**gpt-6-astra high** |
| 08 | **后台执行、真实状态与事件复用**。当前 create_task 绑定回复生命周期。 | 新增可恢复的后台执行/租约与状态查询，沿现有 AgentScope SSE 投影 Stage/Tool 事件，必要时加入按 Execution 查询/回放端点；不先建第二套前端或复杂 MQ。 | 07；`application/`、`infrastructure/`、`demo/agentscope_app.py`、`demo/upload_reply.py`、`tests/integration/test_demo_web_http.py` | 命令立即返回 execution_id/QUEUED，断线后查真实状态，单实例重启后有明确恢复/失败语义，事件不丢重要终态；`uv run pytest tests/integration/test_demo_web_http.py tests/unit/test_persistence.py -q`。 | 崩溃恢复、重复执行、取消一致性；**gpt-6-astra high** |
| 09 | **Web 展示与 CASE-01～06 回归验收**。十步卡片和 SSE 已有，缺四阶段/版本/内层 Tool 展示。 | 增量扩展 Web renderer 与历史选择/状态展示，新增交互 E2E；保留官方 Web 基础和旧 Demo 页面。 | 07–08；`frontend/agentscope-web/frontend/src/components/chat/`、`src/cnlc_agent/demo/presentation.py`、`tests/integration/` | 首次、采样、POR/PERM、模型 B、全量、状态查询六条端到端通过，旧 CLI/Web E2E 仍过；`uv run pytest -q`，`pnpm --dir frontend/agentscope-web/frontend build`（按实际 package 脚本确认）。 | 前后端状态协议、SSE 重连；**gpt-6-sol high** |
| 10 | **真实 Heavy Prediction API 适配**。当前只有专业 Mock，外部预测文件协议未确认。 | 新增 PredictionGateway/输入构建/结果解析/Virtual 投影；修改 INTERPRET 阶段 Provider 选择。保持已验收 qwen-plus ModelGateway、W01～W10 顺序和报告模板。 | 09 + 外部预测协议；`providers/prediction/`、`application/`、`tools/`、`tests/integration/` | Heavy API 每 Execution 一次，Virtual 来源一致、失败可追踪；`uv run pytest tests/integration/test_prediction_provider.py -q`（新增后）及全量 pytest。 | 输入文件/返回字段/超时/幂等待确认；**gpt-6-astra high** |
| 11 | **真实 Report API 适配**。当前报告完全本地生成，外部报告协议未确认。 | 新增 ReportProvider Gateway/Artifact 引用；修改 REPORT 阶段 Provider 选择。保持 ReportAssembler 事实归一化、本地模板后备能力、专业解释流程与 ModelGateway。 | 06、09 + 外部报告协议；`providers/report/`、`reports/`、`application/`、`tests/integration/` | 每个 Execution 有独立报告版本，外部失败保留 InterpretationResult；`uv run pytest tests/integration/test_report_provider.py tests/unit/test_reports.py -q`（新增后）及全量 pytest。 | 报告返回路径/格式及访问控制待确认；**gpt-6-sol high** |

各 Task 的“明确不修改内容”以表内所列为主；特别是 03 不改 Workflow 处理器/Agent/Web，04 不改十步顺序和三个核心 Agent 职责，05 不改已验收 ModelGateway 或引入未经确认的专业公式，06 不改报告事实归一化和 Tool Contract，07 不改专业 Agent/底层 Tool 算法，08 不新建复杂 MQ 或重做前端，09 不改专业 Workflow/模型网关，10–11 不改变 W01～W10 顺序或替换 qwen-plus Gateway。每个 Task 均独立提交，外部协议未确认时只保留接口与 Mock，不提前宣称真实联调完成。

### 第一个真正编码 Task

**先做 Task 01：在现有 TaskRepository/TaskRow 边界增加版本化 Execution，并把 `InterpretationState` 明确作为“一次 Execution 的状态快照”，保持 `run_well_interpretation`、W01～W10 和旧 CLI 行为不变。** 具体首个改点是 `application/ports.py::TaskRepository` 与 `infrastructure/database.py::TaskRow/PostgreSQLTaskRepository` 对多 Execution 的读写契约，再给内存实现、迁移和测试补同一语义。理由：没有不会覆盖历史的持久版本边界，后续 Override、局部重跑、报告、状态与事件都无法可靠归属；现有 `workflow_execution_id` 足以作为兼容关联种子，无需先造一套新 Workflow。

## 9. 风险、Architecture Issue 与未完成依赖

1. **Architecture Issue AI-01：四阶段语义与 W10 位置。** 涉及 `docs/01`、`docs/04`、`docs/05`、`workflows/steps.py`、`application/service.py`。现设计把“报告生成”列在 W10，但代码 W10 仅最终检查，报告在 Workflow 外；若把 W10 整体归 REPORT，会使仅重跑报告也重新检查/改写解释状态。影响 StageRun 映射、局部重跑和验收。建议明确 W10 属 INTERPRET 收尾，REPORT 从 Service 报告链起；四阶段只是执行视图，不变更十步顺序。**不阻塞本分析，编码 Task 04 前需确认。**
2. **Architecture Issue AI-02：采样间隔与 POR/PERM 的专业语义。** 涉及 W03、Mock Tool、预测输入和 `RawData`。当前无重采样实现、正式输入文件协议或 POR/PERM Override 写入规则；若只修改展示值将造成虚假重跑。建议一期清楚限定为参数感知 Mock/输入版本验证，真实重采样和 Heavy API 文件映射待领域规则与供应商协议确认。**不阻塞 Mock 交互骨架；阻塞“真实计算已改变”宣称及 Task 10 联调。**
3. **Architecture Issue AI-03：请求级流式与后台任务生命周期。** 涉及 `demo/upload_reply.py`、`application/runtime.py`、AgentScope RedisStorage。当前用户中断会取消请求内 Tool，上传临时文件也随之删除；长任务目标要求 Execution 跨请求持续并可查。建议先持久输入与 Execution，再设计任务租约/状态恢复并复用 AgentScope SSE 投影。**不阻塞本分析；Task 08 必须解决。**
4. 当前文档的 Execution/StageRun `COMPLETED/REUSED/QUEUED` 与既有 `StepStatus.SUCCESS/WARNING/SKIPPED` 不是一一同义。应分别定义业务步骤终态、执行生命周期、计划动作之间的映射，不把同义状态散落新增到各模块。
5. 已验收的真实 PostgreSQL/Redis 与公网 qwen-plus 能力有代码和历史测试记录，但本次环境只验证离线路径；连接凭据与外部 API 契约不在仓库。Web 上传原始 LAS/GDSX/CSV 仍不支持；正式井数据 Schema 未定。后续真实服务、Heavy API 和专业规则验收依赖项目方输入。

## 10. 本次验证与范围

- `git branch --show-current`：`codex/interactive-agent-2026-09-25`；独立工作树起始无未提交改动。原路径工作区位于 `demo/2026-09-22` 且有他人未提交改动，本次未修改该工作区文件。
- `uv run pytest -q`：**92 passed，4 skipped，1 warning**。跳过项：1 项公网模型 opt-in，3 项真实 PostgreSQL/Redis 服务 opt-in。Warning 来自上游 Starlette/AnyIO 弃用提示。
- 本 Task 仅新增本分析文档；未修改业务代码、测试、依赖、迁移、Web 或任何既有文档。未运行公网模型、真实数据库或重型 API。
