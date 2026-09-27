# Task 10.5-A：Operation 领域模型与 Capability Catalog

## 背景与任务边界

将无限自然语言输入所表达的意图限定为有限、结构化、可验证的操作语义。
本阶段仅建立语义契约与能力目录，提供后续解析和校验的输入类型；没有新增自然语言解析器。
仓库开始时不存在 Task 10.5 总体设计文档，本次以任务说明和既有 Current Design 为依据。

三层独立：

1. Operation Schema 描述系统能识别什么。
2. Operation Capability 描述业务当前开放什么。
3. Handler / Tool 描述代码实际能做什么。

可识别范围可以大于可执行范围。未实现或未确认的领域操作不归为
`OUT_OF_DOMAIN`（领域外请求）。不能唯一解析时，后续解析层应要求澄清。

## Git 基线

- 日期：2026-09-27。
- 稳定基线：`origin/codex/demo-2026-10-31`。
- 基线 HEAD：`5e54d4c8ea98cccec6dde4c446bd10e9f2e771a1`。
- 开发分支：`codex/task-10-5-operation-understanding`。
- 开始 HEAD：`5e54d4c8ea98cccec6dde4c446bd10e9f2e771a1`。
- 开始时执行 `git fetch origin`，确认本地稳定分支与远端稳定基线一致。
  开发分支此前在本地和远端均不存在，已从该远端 HEAD 创建并设置独立 upstream。
- 已跟踪文件干净；已有 `.idea/`、两份 `demo_output` JSON、
  `docs/gdsx-tool-function-inventory.md` 未跟踪，本任务保留原状且不提交。
- 单一提交：`feat: add operation semantic models and capability catalog`，SHA 见完成报告。
- 不修改 main 或稳定基线分支；不创建 PR。

## 文件范围

新增：

- `src/cnlc_agent/demo/operation_models.py`
- `src/cnlc_agent/demo/operation_capabilities.py`
- `tests/unit/test_operation_models.py`
- `tests/unit/test_operation_capabilities.py`
- `docs/tasks/010-5a-operation-models.md`

修改：

- `docs/11-status-enum-glossary.md`

## 现有类型与职责复用

复用 `domain.models.Contract` 与 `JsonObject`，继续禁止额外字段和非有限数值。
直接引用 `demo.task_context.TaskReference`，没有平行任务引用类型。
现有 InteractionPhase、InteractionDecision、PendingClarification、InteractionPolicy、
ExecutionStatus、StepStatus、TaskCommandRunner、任务工具和 ReAct 行为保持不变。

使用 `demo/` 下独立模块与现有交互语义保持邻近，避免将依赖交互 TaskReference 的类型反向放入 Domain。
新模块不导入 AgentScope，不绑定数据库或 Redis；没有将目录接入现有运行链路。

## 核心模型

完整枚举代码、中文名称和语义见 [枚举表第 18～30 节](../11-status-enum-glossary.md#18-inputclassification输入性质task-105-a)。

| 模型 | 职责与边界 |
| --- | --- |
| InputClassification | 输入性质与业务 Action 分离，能力询问不触发执行。 |
| ActionType / TargetType | 有限的动作和目标集合；不把深度或层号放入目标名称。 |
| OperationScopeKind / OperationScope | 六种范围的判别联合，拒绝字段混用。 |
| ExecutionReference | 七种版本引用，序号严格为正整数，ID 非空，选择条件互斥。 |
| ValueSpec | 显式 mode、value、unit，区分绝对值、百分点增量与比例变化。 |
| OperationConstraint | 组合用户限制；排除模型和范围有各自结构化载荷。 |
| OperationParameters | 数值、参数名、方法和模型标识结构化，扩展载荷使用 JsonObject。 |
| OperationInputReference | 其他节点输出与 Execution 引用互斥，不预先分配执行 ID。 |
| OperationContext | 共享任务、版本与范围；缺失值保持未指定。 |
| OperationNode / OperationEdge | 描述动作与顺序、数据、比较关系。 |
| OperationCondition / OutputRequirement | 保存用户条件和输出要求，不执行条件表达式。 |
| OperationPlan | 输入性质、共享上下文、节点、边、条件、保存意向、输出要求和原指令。 |
| OperationResolution | 结果、置信程度、计划、证据、冲突、缺失信息、告警与建议。 |

范围使用稳定 interval_id，多个层段不接受空集合；深度基准显式提供，区间要求 top 小于 bottom，
单位固定为米，允许带符号的海拔基准深度。FilterSetScope 的 resolved_ids 为 null 时表示未解析，
空列表表示解析后没有匹配，非空列表可保存冻结集合；本阶段不执行筛选。

`TASK_CURRENT`（任务当前最新版本）与 `ACTIVE_BASE`（当前工作基线版本）分别建模，
不实现其查询或会话管理。只有 `SEQUENCE`（按版本序号指定）携带 sequence，
只有 `EXECUTION_ID`（按明确执行 ID 指定）携带 execution_id。

数值示例：

| 用户表达 | mode（方式） | value | unit |
| --- | --- | --- | --- |
| 孔隙度改成 16% | `ABSOLUTE`（设置成绝对值） | 16 | % |
| 孔隙度提高 2 个百分点 | `DELTA`（增加 / 减少绝对量） | 2 | percentage_point |
| 孔隙度提高 2% | `PERCENT_CHANGE`（按比例变化） | 2 | % |

模型不换算单位或猜测专业阈值。既有 InterpretationOverride 仍是实际参数修改的唯一命令契约。
例如采样间隔可以表达为井目标加参数名 sampling_interval，不新增平行 Override。

复合意图“把第5层孔隙度改成0.16，重新算一下，再和上一版比较”可表达为：

```text
op1 MODIFY_RESULT（人工修改派生结果）
  ↓ DATA_DEPENDENCY（数据依赖）
op2 RECALCULATE（重新计算）
  ↓ COMPARE_DEPENDENCY（比较依赖）
op3 COMPARE（比较结果） ← PREVIOUS（上一版本）
```

测试直接提供示例稳定层段 ID，不实现“第5层”的解析。多个 Operation 可以在后续被合并为一次执行，
比较也可以保持只读；本 Schema 没有节点与 Execution 一一对应的假设。

persist_mode 必填：`PREVIEW`（预览 / 临时结果）或 `CREATE_VERSION`（创建正式新版本），
没有原地覆盖正式历史的模式。计划可包含零个节点以保存澄清或领域外输入。
缺失引用、重复节点、边端点归属、环、约束冲突、跨任务写入和动作参数兼容性均留给后续
PlanValidator；Pydantic 构造成功不是执行许可。

## Capability Catalog

OperationCapability 包含 action、status、allowed_scopes、description、handler_name、
business_note、supported_parameters；对象只读，避免调用方意外改变共享配置。
OperationCatalog 提供 get、is_executable、list_capabilities 和 with_status。
重复动作声明拒绝；显式空目录保持空，缺失声明的资格判断为 False，未知动作不会隐式启用。

只有 `ENABLED`（已启用）通过 is_executable 判断。
`DISABLED`（已禁用）、`UNVERIFIED`（待业务确认）、`NOT_IMPLEMENTED`（尚未实现）均不可执行。
这仅是能力资格，不验证具体请求。handler_name 只是对现有实现的说明，不动态加载或调用函数。
调用方仍需校验目标、范围、参数、版本、归属、当前状态和业务限制。

with_status 返回新的目录实例，不改 Operation Schema、旧实例或真实 Tool 注册。
测试覆盖人工覆盖改为已禁用、比较从尚未实现改为已启用，以及现有状态查询的关闭。
未来真正启用尚未实现的动作，还必须提供匹配的处理实现、范围和参数支持；切换开关本身不会生成能力。

### 默认 ENABLED（已启用）：4 项

这四项仅声明 `WHOLE_WELL`（整井范围）。开放依据来自真实代码，不是未来设计推断。

| Action | 现有实现 | 当前开放边界 |
| --- | --- | --- |
| `FULL_INTERPRET`（首次 / 整井解释） | run_well_interpretation / TaskCommandRunner.start_uploaded | 已校验资料或 Fixture 首次解释；对应 `START`（开始解释），不代替旧全量重跑动作。 |
| `STATUS`（查询执行状态） | get_interpretation_status / TaskCommands.status | 任务当前持久 Execution 状态；不承诺任意历史状态选择。 |
| `REPORT`（查看 / 生成报告） | get_interpretation_report / TaskCommands.report | 只读取已有当前或受控历史报告；独立报告生成请求未映射。 |
| `MODIFY_PARAMETER`（修改计算参数） | modify_well_interpretation / InterpretationOverride | 仅 por、perm、sampling_interval、prediction_model 的现有绝对设置，应用层决定重跑范围。 |

参数修改不开放相对增量、比例变化、Rw、Archie 参数、截止值、直接修改最终解释结果或局部重算。
REPORT 的广义 Schema 可表达生成意图，但当前 Capability 只声明读取子集，未来适配层不得仅凭动作名放行。

### 默认 DISABLED（已禁用）：0 项

现有文档没有对新增候选动作给出明确业务永久禁用决定，因此不自行补造禁用规则。
目录已支持随业务确认关闭能力。

### 默认 UNVERIFIED（待业务确认）：15 项

| Action | 依据 |
| --- | --- |
| `MODIFY_RESULT`（人工修改派生结果） | 派生值人工修订规则和审计边界待确认。 |
| `SWITCH_METHOD`（切换方法） | 方法清单及切换条件待确认。 |
| `COMPARE`（比较结果） | 指标与验收口径未确认，且无比较引擎。 |
| `SEGMENT_EDIT`（修改层段） | 拆层、合层及边界规则待确认。 |
| `OVERRIDE`（人工最终解释覆盖） | 是否允许以及最终结论修订规则待确认。 |
| `RESTORE_VERSION`（恢复历史版本） | 恢复与工作基线的业务语义待确认。 |
| `SCENARIO`（方案试算） | 隔离方式和试算生命周期待确认。 |
| `COMMIT_SCENARIO`（应用试算结果） | 试算转正式结果的采用规则待确认。 |
| `CANCEL_EXECUTION`（取消执行） | 外部调用副作用和取消终态待确认；当前断开 SSE 不取消 Worker。 |
| `ACCEPT_RESULT`（接受结果） | 人工接受流程与权限待确认。 |
| `REJECT_RESULT`（拒绝结果） | 拒绝后的处理流程待确认。 |
| `MARK_FINAL`（标记最终版本） | 最终版本选择和审批规则待确认。 |
| `MARK_REVIEW`（标记待复核） | 人工标记与既有自动复核状态关系待确认。 |
| `REPLACE_INPUT`（替换既有输入资料） | 同任务替换输入的规则待确认，当前重传仍创建新 Task。 |
| `ADD_EVIDENCE`（补充验证证据） | 增量证据输入契约和影响范围待确认。 |

### 默认 NOT_IMPLEMENTED（尚未实现）：11 项

| Action | 依据 |
| --- | --- |
| `QUERY`（查询结果） | 交互状态文档明确通用细粒度查询尚未开放；状态和报告使用独立动作。 |
| `EXPLAIN`（解释原因 / 溯源） | 层段原因、证据查询在交互文档中明确尚未开放。 |
| `RECALCULATE`（重新计算） | 细粒度目标重算属于后续能力，不能强行映射到整井重跑。 |
| `REINTERPRET`（重新解释） | 已有 `FULL_RERUN`（全量重跑），但无覆盖新目标和范围语义的独立适配。 |
| `SWITCH_MODEL`（切换预测模型） | prediction_model 可以通过参数修改，但无独立新动作适配。 |
| `VALIDATE`（综合验证） | W09 已在流程内执行，无独立操作入口。 |
| `HISTORY`（查询历史） | Read API 已有历史读取，无 Operation 查询入口。 |
| `PAUSE_EXECUTION`（暂停执行） | 总体架构明确为后续能力。 |
| `RESUME_EXECUTION`（恢复执行） | 总体架构明确为后续能力。 |
| `RETRY_EXECUTION`（重试执行） | 现有显式全量重跑不等价于独立重试策略。 |
| `NEW_WELL`（新井资料） | 上传创建新 Task 已存在，无独立文件语义处理入口。 |

“尚未实现”指新 Operation 语义的独立处理能力；不否认表中已有的底层功能。
未启用动作的 allowed_scopes 为空，表示尚未开放范围，不代表任意范围。

## 测试与质量结果

| 命令 | 结果 |
| --- | --- |
| `uv run pytest -q tests/unit/test_operation_models.py tests/unit/test_operation_capabilities.py` | 62 passed |
| `uv run pytest -q tests/unit/` | 287 passed，包含原有 225 项及新增 62 项 |
| `uv run pytest -q tests/integration/test_interaction_robustness.py tests/integration/test_task_react.py` | 61 passed |
| `uv run ruff check src/cnlc_agent/demo/operation_models.py src/cnlc_agent/demo/operation_capabilities.py tests/unit/test_operation_models.py tests/unit/test_operation_capabilities.py` | 通过 |
| `uv run mypy src/cnlc_agent/demo/operation_models.py src/cnlc_agent/demo/operation_capabilities.py` | 通过，2 个生产文件无错误 |
| `git diff --check` | 通过 |

覆盖稳定枚举序列化、单节点和复合计划往返、目标与范围分离、全部版本选择器及非法值、深度范围、
非空层段集合、筛选描述和冻结 ID、三种数值语义、组合限制、依赖边、四种能力状态、业务开关与
Schema 解耦，以及既有 TaskReference、InteractionPhase、InteractionDecision 的兼容回归。
JSON Schema 可导出，并复用唯一 TaskReference 定义。

全量单元测试包含 Task 10.3 的 interaction_state / task_context / task_tools / task_commands，
以及 Task 10.4 的 conversation_persistence / migrations 回归。ReAct 集成测试使用既有 Mock
模型验证交互、澄清、复合写保护和任务工具契约，未新增第二套自然语言解析器。

本次不涉及 Web 可见行为，因此没有新增页面验收。未运行真实 PostgreSQL / Redis 集成测试或全仓
ruff / mypy；本任务未修改这些基础设施，定向质量门禁均通过。Task 10.4 已记录全仓既有 ruff 222 个、
mypy 243 个错误，本次未复测该数量，也未扩大范围清理既有债务。

## 未实现范围与后续依赖

- 未实现 ReferenceResolver、Scope Resolver、ContextResolver、PlanValidator 或交互裁决映射。
- 未实现 Active / View Context、专业依赖图、影响分析、细粒度重算、比较引擎、试算引擎、层段编辑、
  条件链执行、跨任务复合写或额外 Handler。
- 未实现输入版本替换或 Evidence Pipeline；正式井资料仍遵循现有契约：Pending final well-data schema.
- 未新增 migration，未修改数据库 Schema 或 Conversation persistence。
- 未修改 W01–W10、ReAct、Task Tools、核心 Agent、专业 Tool 或既有运行行为。
- 后续阶段需要基于本契约实现引用与上下文解析、计划校验、明确业务能力规则并对接真实处理入口；
  本任务到 10.5-A 结束，没有提前实现 10.5-B～F 或 Task 11。

## Architecture Issue

No Architecture Issue found.
