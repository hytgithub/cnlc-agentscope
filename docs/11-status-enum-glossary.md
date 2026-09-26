# 状态、枚举与节点中文说明（Current Design）

本文统一解释项目当前代码和设计文档中常见的英文枚举、状态、动作码和流程节点。英文代码值保留用于与代码、数据库、日志和测试对应；面向人的文档统一写成 `CODE（中文名称/含义）`。

后续新增枚举、状态节点或稳定动作码时，应同步更新本文，并在其他 Current Design 文档首次出现时补充中文说明。

## 1. StepStatus（Workflow 步骤状态）

| 代码值 | 中文名称 | 含义 |
| --- | --- | --- |
| `PENDING` | 等待执行 | 步骤尚未开始。 |
| `RUNNING` | 正在执行 | 当前步骤正在运行。 |
| `SUCCESS` | 执行成功 | 步骤正常完成。 |
| `WARNING` | 完成但有告警 | 步骤完成，但存在非阻断问题。 |
| `FAILED` | 执行失败 | 步骤发生错误，不能按正常成功路径继续。 |
| `BLOCKED` | 被阻断 | 缺少关键资料或前置条件，当前步骤无法执行。 |
| `REVIEW_REQUIRED` | 需要人工复核 | 自动流程不能安全给出确定结果，需要人工检查。 |
| `SKIPPED` | 已跳过 | 根据明确流程规则允许跳过该步骤。 |

`StepStatus` 描述 W01～W10 节点状态，不等同于后台 `ExecutionStatus`。

## 2. ExecutionStatus（后台执行状态）

| 代码值 | 中文名称 | 含义 |
| --- | --- | --- |
| `QUEUED` | 排队等待 | Execution 已创建，等待 Worker 领取。 |
| `RUNNING` | 正在执行 | Worker 已领取并持有有效租约。 |
| `SUCCESS` | 执行成功 | 本次 Execution 正常结束。 |
| `WARNING` | 完成但有告警 | 本次 Execution 已完成，但存在告警。 |
| `FAILED` | 执行失败 | 本次 Execution 因错误终止。 |
| `BLOCKED` | 被阻断 | 缺少关键数据或条件，本次 Execution 无法继续。 |
| `REVIEW_REQUIRED` | 需要人工复核 | 自动流程停止，等待人工复核。 |

Execution 没有 `PENDING` 和 `SKIPPED`；这两个值属于 Workflow 步骤状态。

## 3. ValidationStatus（W09 多源综合验证结论）

| 代码值 | 中文名称 | 含义 | 当前处理 |
| --- | --- | --- | --- |
| `CONSISTENT` | 证据一致 | 岩心、录井、试油、邻井等验证证据与当前测井解释总体一致，没有发现实质冲突。 | 可继续后续流程。 |
| `PARTIAL_CONFLICT` | 部分冲突 | 一部分外部证据与当前解释不一致，但不足以认定结论整体失效。 | 当前实现提升为 `WARNING`（完成但有告警）。 |
| `SERIOUS_CONFLICT` | 严重冲突 | 外部验证证据与当前解释存在明显、关键冲突，不能安全继续给出确定最终结论。 | 当前实现进入 `REVIEW_REQUIRED`（需要人工复核），不自动回退重跑。 |
| `INSUFFICIENT_EVIDENCE` | 证据不足 | 可用于验证当前解释的独立证据数量、覆盖范围或可信度不足，无法确认当前解释是否可靠。它**不等于“解释一定错误”**，而是“目前证据不足以验证”。 | 当前实现进入 `REVIEW_REQUIRED`（需要人工复核）。 |

特别说明：`INSUFFICIENT_EVIDENCE`（证据不足）与 `SERIOUS_CONFLICT`（严重冲突）不同。前者是“证据不够”，后者是“已有证据明确冲突”。

## 4. W01～W10（固定业务节点）

| 节点 | 中文业务含义 |
| --- | --- |
| `W01` | 任务初始化与原始资料加载 |
| `W02` | 数据完整性检查 |
| `W03` | 数据预处理与质量控制 |
| `W04` | 岩性识别 |
| `W05` | 储层识别与物性评价 |
| `W06` | 流体识别 / 含水饱和度相关处理 |
| `W07` | 油气水层分类 |
| `W08` | 层段划分与有效厚度计算 |
| `W09` | 多源资料综合验证 |
| `W10` | 最终一致性检查 |

报告生成发生在 W01～W10 之后，不把报告伪装成新的 Workflow 节点。

## 5. InteractionPhase（交互阶段）

| 代码值 | 中文名称 | 含义 |
| --- | --- | --- |
| `NO_TASK` | 当前无任务 | Session 尚未绑定可操作的解释 Task。 |
| `READY` | 可接受新操作 | 当前 Task 没有活跃执行，可进入 Application 做进一步校验。 |
| `ACTIVE` | 当前有活跃执行 | 当前 Execution 为 `QUEUED`（排队等待）或 `RUNNING`（正在执行）。 |
| `NEED_CLARIFICATION` | 等待用户澄清 | 存在有效的短期 PendingClarification，等待用户补齐缺失信息。 |

## 6. InteractionDecision（交互裁决）

| 代码值 | 中文名称 | 含义 |
| --- | --- | --- |
| `ALLOW` | 允许执行 | 可以继续调用写操作并进入 Application。 |
| `READ_ONLY` | 只读查询 | 允许查询状态或报告，不创建新 Execution。 |
| `CLARIFY` | 需要澄清 | 信息不足或多个操作互相冲突，先询问用户，不执行写操作。 |
| `REJECT` | 拒绝本次操作 | 当前状态或能力不允许执行，返回稳定原因，不创建 Execution。 |

文档中的 `EXECUTE / QUERY / CLARIFY / REJECT` 是面向人的交互结果描述；代码 Policy 值为 `ALLOW / READ_ONLY / CLARIFY / REJECT`。

## 7. TaskReferenceKind（会话任务引用）

| 代码值 | 中文名称 | 含义 |
| --- | --- | --- |
| `CURRENT` | 当前井 / 当前任务 | 使用 active task；不可恢复时回退到当前 Session 最近绑定的 Task。 |
| `PREVIOUS_TASK` | 上一口井 / 上一个任务 | 使用 active task 之前绑定的 Task。 |
| `WELL_ID` | 按井号指定 | 在当前 Session 内按井号选择最近创建的 Task。 |
| `TASK_ID` | 按可信任务号指定 | 仅在 SessionTaskBinding 验证归属后使用。 |

`PREVIOUS_TASK`（上一口井）与报告的 `PREVIOUS`（当前井上一版）是不同概念。

## 8. Report Selector（报告版本选择）

| 代码值 | 中文名称 | 含义 |
| --- | --- | --- |
| `CURRENT` | 当前版本报告 | 当前 Task 的 current_execution_id 对应报告。 |
| `PREVIOUS` | 上一版报告 | 当前 Task 当前 Execution 之前的上一 Execution 报告，不跨井。 |
| `LATEST_SUCCESSFUL` | 最近成功报告 | 当前 Task 最近一个成功形成可用报告的 Execution。 |

## 9. Task-level Intent / Action（任务级意图）

| 代码值 | 中文名称 | 含义 |
| --- | --- | --- |
| `START` | 开始解释 | 创建 Task / InputVersion / 首次 Execution。 |
| `MODIFY` | 修改参数并重跑 | 修改受支持参数并创建新 Execution。 |
| `FULL_RERUN` | 全量重跑 | 从 W01 开始创建新的完整 Execution。 |
| `STATUS` | 查询状态 | 读取当前持久执行事实。 |
| `GET_REPORT` | 查询报告 | 按受控版本选择读取报告。 |
| `OUT_OF_DOMAIN` | 领域外请求 | 不属于单井常规测井解释及其任务操作，不调用测井 Tool。 |

## 10. ExecutionStage 与 PlanAction（执行规划）

| ExecutionStage | 中文名称 | 对应步骤 |
| --- | --- | --- |
| `DATA_DECODE` | 数据加载 / 解编阶段 | W01 |
| `PREPROCESS` | 数据预处理与质量控制阶段 | W02～W03 |
| `INTERPRET` | 专业解释阶段 | W04～W10 |
| `REPORT` | 报告生成阶段 | W01～W10 之后 |

| PlanAction | 中文名称 | 含义 |
| --- | --- | --- |
| `RUN` | 重新执行 | 本次 Execution 需要重新运行该阶段。 |
| `REUSE` | 复用已有结果 | 从可信成功来源复用该阶段结果。 |

## 11. PlanningReason（执行规划原因）

| 代码值 | 中文名称 | 含义 |
| --- | --- | --- |
| `INITIAL` | 首次解释 | 首次创建执行版本。 |
| `FULL_RERUN` | 显式全量重跑 | 明确要求全量重跑。 |
| `NO_REUSABLE_SOURCE` | 无可复用来源 | 没有满足条件的成功 Execution 可作为复用基线。 |
| `INPUT_CHANGED` | 输入资料变化 | InputVersion 内容摘要发生变化。 |
| `OVERRIDE_CHANGED` | 参数变化 | 受支持的 Override 参数发生变化。 |
| `REPORT_ONLY` | 仅重新生成报告 | 专业结果可复用，只创建新的报告版本。 |
| `LEGACY_MIGRATED` | 历史数据迁移 | 由旧版数据迁移形成的兼容记录。 |

## 12. ExecutionTrigger（Execution 触发类型）

| 代码值 | 中文名称 | 含义 |
| --- | --- | --- |
| `INITIAL` | 首次触发 | Task 创建后的首个 Execution。 |
| `RERUN` | 重跑触发 | Task 已存在后的后续 Execution。 |

## 13. ToolRunStatus（专业工具调用状态）

| 代码值 | 中文名称 | 含义 |
| --- | --- | --- |
| `RUNNING` | 工具正在执行 | ToolRun 已创建但尚未结束。 |
| `SUCCESS` | 工具执行成功 | Tool 正常完成。 |
| `WARNING` | 工具完成但有告警 | Tool 返回有效结果，同时带有非阻断告警。 |
| `FAILED` | 工具执行失败 | Tool 调用失败并记录稳定错误码。 |

## 14. ToolExecutionMode（工具执行来源模式）

| 代码值 | 中文名称 | 含义 |
| --- | --- | --- |
| `MOCK` | Mock / 模拟执行 | 返回演示或测试数据，不代表真实专业计算。 |
| `REAL` | 真实执行 | 由真实工具、算法或真实外部服务执行。 |
| `VIRTUAL` | 虚拟 / 逻辑执行 | 代码定义的虚拟调用模式，不应被解释为独立真实外部调用。 |
| `DERIVED` | 派生结果 | 从已完成的共享批量调用结果中投影/拆分得到，本身不是一次独立外部调用。 |

当前 company_mock 中，批量入口记录为 `MOCK`（模拟执行），细分逻辑结果记录为 `DERIVED`（派生结果）。

## 15. MissingData Importance（缺失资料重要级别）

| 代码值 | 中文名称 | 含义 |
| --- | --- | --- |
| `Required` | 必需资料 | 缺失会阻断对应关键步骤。 |
| `Recommended` | 推荐资料 | 缺失通常不阻断流程，但会降低证据充分性或可靠性并产生告警。 |
| `Optional` | 可选资料 | 用于增强解释或验证，缺失原则上不影响主流程。 |

业务文档中的 `PASS / PASS_WITH_WARNING / BLOCKED` 是完整性检查的业务表达，不是新增的 `StepStatus` 枚举。

## 16. Task 10.3 常见稳定错误码

这些不是状态枚举，但在交互文档和测试中频繁出现：

| 错误码 | 中文含义 |
| --- | --- |
| `TASK_NOT_FOUND` | 当前会话没有可操作任务，或任务不属于当前会话。 |
| `REPORT_NOT_FOUND` | 当前选择条件下不存在历史报告。 |
| `REPORT_NOT_READY` | 指定 Execution 尚未形成可用报告。 |
| `NO_EFFECTIVE_CHANGE` | 新参数与当前有效参数相同，没有有效变化。 |
| `EMPTY_OVERRIDE` | 修改请求没有提供可用参数。 |
| `TASK_EXECUTION_ACTIVE` | 当前 Task 已有排队或运行中的 Execution，禁止并发写操作。 |
| `STALE_EXECUTION_PLAN` | 计划生成后 Task 当前版本已变化，需要重新规划。 |
| `PREVIOUS_TASK_NOT_FOUND` | 当前 Session 没有上一口井 / 上一个 Task。 |
| `SESSION_WELL_NOT_FOUND` | 当前 Session 内没有指定井号对应的 Task。 |
| `CLARIFICATION_REQUIRED` | 当前请求信息不足或意图冲突，需要用户补充确认。 |
| `UNSUPPORTED_OPERATION` | 请求属于测井领域，但当前版本尚未开放该操作能力。 |
| `UNSUPPORTED_PARAMETER` | 请求修改的参数不在当前受支持参数契约中。 |

## 17. 文档维护规则

1. 代码值保持英文，方便与日志、API、数据库和测试一一对应。
2. 面向人的文档首次出现状态、枚举、动作码或稳定错误码时，写成 `CODE（中文含义）` 或提供紧邻的中文说明表。
3. 新增或修改枚举时，同步更新本文。
4. 历史 Task 记录可以保留当时原始代码值，但新增加的说明必须引用本文；Current Design 文档不得只列英文枚举而不解释。
