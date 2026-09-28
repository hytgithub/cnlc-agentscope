# 四阶段 Tool 映射与可审计 Mock 集成

## 1. 目的与事实来源

本契约固定现有业务阶段、Workflow Step（工作流步骤）、Tool（工具）和 ToolRun（工具运行事实）
之间的关系。代码中的 `tools/catalog.py` 是 Tool → Step → Business Stage（业务阶段）的单一静态来源；
`STAGE_STEPS` 仍是 Step → Business Stage 的单一来源。Catalog 不进入数据库，也不形成 Tool 版本体系。

四阶段边界保持为：

- `DECODE`（数据解编）= W01；
- `PREPROCESS`（预处理）= W02～W03；
- `INTERPRET`（解释）= W04～W10；
- `REPORT`（报告）= W10 之后的 `ReportAssembler`。

Provider Batch Operation（提供方批量操作）是外部能力分组，不是业务阶段。特别是 provider 的
`report` 操作由 W10 的 `company_report` 使用，因此属于 `INTERPRET`；真正的 `REPORT` 由
`ReportAssembler` 生成候选报告。

## 2. 当前完整执行矩阵

下表描述当前代码实际执行内容。`MOCK`（模拟调用）表示一次 Mock provider 或 Fixture 调用，
`DERIVED`（派生结果）表示从同一批量响应投影结果，不代表独立外部请求。“fixture/其他”中的
Agent 和 ModelGateway 不产生 ToolRun。

| Business Stage | Workflow Step | Execution Kind | Tool Code / 组件 | Execution Mode | Provider Batch | 当前输出 | 失败行为 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| DECODE | W01 | TOOL | `get_well_data` | fixture: `MOCK`；company_mock: `DERIVED` | company_mock: `company_analysis` | `well`、`raw_data`、`requirements` | ToolRun `FAILED`，W01 与 DECODE 失败 |
| DECODE | W01 | TOOL | `company_analysis` | `MOCK` | analysis 的一次物理调用 | 含 `well_data` 的批量结果 | ToolRun `FAILED`，调用方 W01 失败 |
| PREPROCESS | W02 | RULE | 完整性规则 | 无 ToolRun | 无 | 必需曲线完整性结论 | W02 `BLOCKED`，阶段不能确认 |
| PREPROCESS | W03 | TOOL | `check_curve_quality` | fixture: `MOCK`；company_mock: `DERIVED` | company_mock: `company_preprocessing` | `StageResult` QC 结果 | ToolRun `FAILED` 或业务非成功，PREPROCESS 失败 |
| PREPROCESS | W03 | TOOL | `company_preprocessing` | `MOCK` | preprocessing 的一次物理调用 | `qc`、是否执行处理及当前 Mock 数据 | ToolRun `FAILED`，调用方 W03 失败 |
| INTERPRET | W04 | TOOL | `identify_lithology` | fixture: `MOCK`；company_mock: `DERIVED` | company_mock: `company_interpretation` | 岩性 `StageResult` | ToolRun `FAILED`，W04 与阶段失败 |
| INTERPRET | W04～W09 | TOOL | `company_interpretation` | `MOCK` | interpretation 的一次物理调用；同 Execution/输入缓存 | 岩性、物性、Sw、流体、分类、层段和验证的批量结果 | ToolRun `FAILED`，首次消费它的步骤失败 |
| INTERPRET | W05 | TOOL | `evaluate_petrophysics` | fixture: `MOCK`；company_mock: `DERIVED` | company_mock: `company_interpretation` | 物性 `StageResult` | ToolRun `FAILED`，W05 与阶段失败 |
| INTERPRET | W06 | TOOL | `calculate_sw` | fixture: `MOCK`；company_mock: `DERIVED` | company_mock: `company_interpretation` | 含水饱和度 `StageResult` | ToolRun `FAILED`，W06 与阶段失败 |
| INTERPRET | W06 | TOOL | `identify_fluid`（仅 company_mock） | `DERIVED` | company_mock: `company_interpretation` | 流体识别 `StageResult` | ToolRun `FAILED`，W06 与阶段失败 |
| INTERPRET | W06 | AGENT | `InterpretationAgent`（非 company_mock） | 无 ToolRun | 无 | 综合 `calculate_sw` 与模型输出得到流体结果 | 维持现有 Agent / Workflow 失败语义 |
| INTERPRET | W07 | TOOL | `classify_layer`（仅 company_mock） | `DERIVED` | company_mock: `company_interpretation` | 层分类 `StageResult` | ToolRun `FAILED`，W07 与阶段失败 |
| INTERPRET | W07 | AGENT | `InterpretationAgent`（非 company_mock） | 无 ToolRun | 无 | 现有结构化层分类 | 维持现有 Agent / Workflow 失败语义 |
| INTERPRET | W08 | TOOL | `merge_intervals` | fixture: `MOCK`；company_mock: `DERIVED` | company_mock: `company_interpretation` | 层段 `StageResult` | ToolRun `FAILED`，W08 与阶段失败 |
| INTERPRET | W09 | TOOL | `validate_interpretation`（仅 company_mock） | `DERIVED` | company_mock: `company_interpretation` | 验证 `StageResult` | ToolRun `FAILED`，W09 与阶段失败 |
| INTERPRET | W09 | AGENT / RULE | `ValidationAgent` 或 Demo skip（非 company_mock） | 无 ToolRun | 无 | 验证结果或明确跳过状态 | 维持现有冲突与复核语义 |
| INTERPRET | W10 | TOOL | `prepare_report`（仅 company_mock） | `DERIVED` | company_mock: `company_report` | final-check/provider projection | ToolRun `FAILED`，W10 与阶段失败 |
| INTERPRET | W10 | TOOL | `company_report` | `MOCK` | report 的一次物理调用 | 当前本地报告渲染准备事实 | ToolRun `FAILED`，调用方 W10 失败 |
| INTERPRET | W10 | RULE | 本地 structural final check（非 company_mock） | 无 ToolRun | 无 | `final_check` | W10 维持现有失败语义 |
| REPORT | W10 之后 | ASSEMBLER | `ReportAssembler` | 无 ToolRun | 无 | 候选 Markdown 与报告逻辑引用 | `REPORT_GENERATION_FAILED`，REPORT 阶段失败 |

当前 Catalog 有 10 个业务 Tool Code 和 4 个 provider batch Tool Code。company_mock 完整执行时，
10 个业务 ToolRun 都是 `DERIVED`；4 个 batch ToolRun 才代表物理 Mock provider 调用。fixture
配置只装配并实际执行 6 个业务 Tool Code：W01、W03、W04、W05、W06 和 W08 对应 Tool。

## 3. Tool 输入与输出边界

所有 Tool 都接收统一 `ToolInput`：`task_id`、`trace_id`、`well_id`、`step_id` 和
`parameters`。应用运行时的 `parameters` 是当前 `ExecutionContext`，包含 `execution_id`、可选
`input_version_id` 和 `effective_override`。当前输出如下：

| Tool | 当前实际输出 |
| --- | --- |
| `get_well_data` | `well`、`raw_data`、`requirements`。 |
| `check_curve_quality` | Fixture 或共享 batch 中的 QC `StageResult`；当前可带请求采样间隔和是否实际重采样。 |
| `identify_lithology` | 岩性 `StageResult`。 |
| `evaluate_petrophysics` | 物性 `StageResult`；Demo 参数投影沿用现有明确边界。 |
| `calculate_sw` | 含水饱和度 `StageResult`。 |
| `identify_fluid` | 共享 interpretation batch 的流体 `StageResult`。 |
| `classify_layer` | 共享 interpretation batch 的分类 `StageResult`。 |
| `merge_intervals` | 层段 `StageResult`。 |
| `validate_interpretation` | 共享 interpretation batch 的验证 `StageResult`。 |
| `prepare_report` | 共享 report batch 的 final-check `StageResult`；不生成业务报告。 |

四个 `company_*` batch Tool 输出当前 Mock provider 的规范化批量对象。该对象只供同一执行中的
derived Tool 消费，不完整写入 StageToolRunView，也不作为真实公司 API 协议声明。

## 4. Catalog 与运行时防线

`ToolBinding` 包含 `tool_code`、`allowed_steps`、`business_stage`、`description` 和是否为
provider batch。模型校验确保所有 allowed Step 都属于声明的业务阶段，模块加载时确保 Tool Code
唯一。Bootstrap 只校验当前 profile 实际注入的 Tool，因此 fixture 不需要虚构 company_mock 能力。

`ToolCaller.call()` 在建立 ToolRun 和执行 Tool 前调用同一 Catalog。已登记 Tool 在错误 Step 调用时
抛出 `TOOL_STEP_MISMATCH`（工具步骤不匹配），且不会执行 Tool。`company_interpretation` 明确允许
W04～W09，所以局部执行第一次从 W06 请求 batch 是合法调用。Catalog 不负责重试、回滚或降级。

## 5. 物理调用与派生结果关联

company_mock batch 成功后生成 `external_call_id`。`ToolCaller` 将
`ToolOutput.metadata["external_call_id"]` 写入 ToolRun 的 `source_external_call_id`：

1. `company_interpretation` 等物理 batch ToolRun 记录该 ID；
2. 消费共享响应的多个 `DERIVED` ToolRun 记录同一 ID；
3. 页面可按字段识别“一次 provider 调用 → 多个内部结果”，无需解析 `source` 字符串；
4. 完整 provider response 不写入 ToolRun 展示投影。

失败仍由既有层次处理：ToolRun 记录 `FAILED`（失败）和安全错误码/消息；Workflow 产生步骤与阶段
失败、阻断或复核状态。Catalog 不改变这些状态。

## 6. StageToolRunView

`StageToolRunView` 是持久 ToolRun 的查询投影，字段为：

- `tool_run_id`、`execution_id`、`tool_code`、`step_id`、`business_stage`；
- `status`、`execution_mode`、`source`、`external_call_id`；
- `started_at`、`finished_at`、`duration_ms`；
- `error_code`、安全 `error_message`；
- 有界 `input_summary`、`output_summary`。

业务阶段由 `ToolRun.step_id` 和 `STAGE_STEPS` 确定性推导，没有新增数据库 stage 字段。
`StageResultProjector.project()` 按显式 Execution 查询 ToolRun，再按目标 Stage 的 Step 集合过滤；同步
`project_loaded()` 只接受调用方显式传入的 ToolRun，保持纯投影，不访问 Repository。

每个 StageResultView 最多包含 50 个 StageToolRunView，并继续受 128 KiB 总体积限制。单条工具视图
另受 32 KiB、列表长度、文本长度和嵌套深度限制。投影不会重新加载或输出 `raw_data`、
`processed_data`、完整 `depths`、曲线 `values`、完整 provider response、MockFixture、Markdown、
token、authorization 或本地路径。REPORT 当前正确返回 `tool_runs=[]`。

## 7. 查询、历史与隔离

ToolRun 查询始终使用显式 `execution_id`；StageResult 查询仍显式要求 `task_id`、`execution_id` 和
`stage_run_id` 并先校验归属。历史 Execution 可从 PostgreSQL ToolRun 事实重新生成相同审计视图，
不依赖 `CompanyBatchResults` 的进程内缓存；其 `can_confirm=false`。不同井即使拥有相同步骤，也不会
因 Step 过滤而混入彼此 ToolRun。

本任务没有修改 W01～W10 顺序、四阶段边界、Agent、专业算法、LLM Prompt、DatasetRevision
绑定、Retry / Rollback 或 Trace。

No Architecture Issue found.
