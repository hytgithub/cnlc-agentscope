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
| `WAITING_CONFIRMATION` | 等待阶段确认 | 分阶段模式的当前阶段已产生候选结果，Worker lease 已释放，Execution 尚未终结。 |
| `SUCCESS` | 执行成功 | 本次 Execution 正常结束。 |
| `WARNING` | 完成但有告警 | 本次 Execution 已完成，但存在告警。 |
| `FAILED` | 执行失败 | 本次 Execution 因错误终止。 |
| `BLOCKED` | 被阻断 | 缺少关键数据或条件，本次 Execution 无法继续。 |
| `REVIEW_REQUIRED` | 需要人工复核 | 自动流程停止，等待人工复核。 |

Execution 没有 `PENDING` 和 `SKIPPED`；这两个值属于 Workflow 步骤状态。
`WAITING_CONFIRMATION`（等待阶段确认）不是终态，且只用于
`STAGED_CONFIRMATION`（分阶段确认执行）模式。

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

### 17.1 DatasetChangeType 与 Task 11B 稳定错误码

| 代码值 | 中文名称 | 含义 |
| --- | --- | --- |
| `CURVE_SAMPLE_PATCH` | 曲线采样点修改 | 按可信 Base Dataset Revision 修改一个或多个真实 MD 采样点。 |

| 错误码 | 中文含义 |
| --- | --- |
| `DATASET_REVISION_TASK_MISMATCH` | Dataset Revision 不属于请求 Task。 |
| `DATASET_CURVE_NOT_FOUND` | Base Revision 中不存在请求曲线。 |
| `DATASET_CURVE_UNIT_MISMATCH` | 请求曲线单位与可信基线不一致。 |
| `PATCH_DEPTH_NOT_FOUND` | 请求 MD 深度未精确命中真实采样轴。 |
| `DUPLICATE_PATCH_CURVE` | 同一请求重复声明曲线。 |
| `DUPLICATE_PATCH_SAMPLE` | 同一曲线重复修改相同采样点。 |
| `NO_EFFECTIVE_DATASET_CHANGE` | 所有目标值均与前值相同，没有有效修改。 |
| `INVALID_DATASET_REVISION_CHAIN` | 父版本、ChangeSet、Root 或 lineage 关系无效。 |
| `DATASET_REVISION_CHAIN_TOO_DEEP` | lineage 超过安全物化深度。 |
| `INVALID_CHANGE_SET_DIGEST` | ChangeSet 摘要与稀疏内容不一致。 |

## 18. InputClassification（输入性质，Task 10.5-A）

以下语义契约仅建模，尚未接入 ReAct、解析器或执行层。输入性质不等于业务动作。

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `EXECUTION_REQUEST` | 执行请求 | 请求产生业务执行或修改。 |
| `READ_REQUEST` | 只读请求 | 请求读取已有业务事实。 |
| `CLARIFICATION_REPLY` | 澄清回复 | 补充此前尚不明确的信息。 |
| `CORRECTION` | 修正上一输入 | 纠正前一输入的含义，不自动撤销已执行事实。 |
| `CONFIRMATION` | 确认 / 采用 | 表达接受或采用意向，不直接形成写操作。 |
| `CANCELLATION` | 取消 / 撤销请求 | 表达取消意向，需区分取消交互与取消后台执行。 |
| `CAPABILITY_QUERY` | 能力询问 | 询问能否操作；不能直接当成执行请求。 |
| `META_REQUEST` | 系统操作请求 | 面向系统交互或管理的请求。 |
| `OUT_OF_DOMAIN` | 领域外请求 | 不属于测井解释及其任务操作；已识别但未开放的动作不属于此类。 |

## 19. ActionType（操作业务动作）

这是 Operation 的有限语义空间，不替换第 9 节的现有任务级意图；可识别不意味着可执行。

Task 10.5-E1 补充 `FULL_RERUN`（全量重跑）：已有授权 Task 的整井重跑，继承现有有效参数。
其 Capability 为 ENABLED（已启用），handler 为 rerun_well_interpretation；不能代替首次解释或局部重新解释。

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `QUERY` | 查询结果 | 读取指定业务对象结果。 |
| `EXPLAIN` | 解释原因 / 溯源 | 追溯结果依据、证据和来源。 |
| `MODIFY_RESULT` | 人工修改派生结果 | 表达修改派生值的意图，不修改原始曲线。 |
| `MODIFY_PARAMETER` | 修改计算参数 | 表达参数设置，实际支持范围仍以原命令契约为准。 |
| `RECALCULATE` | 重新计算 | 表达对指定目标重算，不隐式升级为整井全量重跑。 |
| `REINTERPRET` | 重新解释 | 基于目标、范围和版本重新解释。 |
| `SWITCH_METHOD` | 切换方法 | 请求更换计算或解释方法。 |
| `SWITCH_MODEL` | 切换预测模型 | 请求更换专业预测模型，不等同于更换外层语言模型。 |
| `COMPARE` | 比较结果 | 比较明确的版本或操作输出。 |
| `SEGMENT_EDIT` | 修改层段 | 表达层段或层界编辑意图。 |
| `VALIDATE` | 综合验证 | 请求独立验证已有解释。 |
| `OVERRIDE` | 人工最终解释覆盖 | 表达人工最终结论修订，不表示数据库原地覆盖。 |
| `RESTORE_VERSION` | 恢复历史版本 | 表达恢复指定历史结果的意图；具体版本行为待确认。 |
| `SCENARIO` | 方案试算 | 表达临时方案计算意图。 |
| `COMMIT_SCENARIO` | 应用试算结果 | 表达将试算结果采用为正式结果的意图。 |
| `REPORT` | 查看 / 生成报告 | 表达报告需求；当前能力声明只开放已有报告读取。 |
| `HISTORY` | 查询历史 | 查询解释版本历史。 |
| `FULL_INTERPRET` | 首次 / 整井解释 | 当前能力声明对应首次解释；不替代现有全量重跑动作。 |
| `STATUS` | 查询执行状态 | 读取持久执行进度和状态。 |
| `CANCEL_EXECUTION` | 取消执行 | 请求停止后台执行；不同于取消澄清。 |
| `PAUSE_EXECUTION` | 暂停执行 | 请求暂停后台业务执行。 |
| `RESUME_EXECUTION` | 恢复执行 | 请求恢复已暂停执行。 |
| `RETRY_EXECUTION` | 重试执行 | 表达重试意图，重试策略不由语义模型决定。 |
| `ACCEPT_RESULT` | 接受结果 | 表达人工接受结果的决定。 |
| `REJECT_RESULT` | 拒绝结果 | 表达人工拒绝结果的决定。 |
| `MARK_FINAL` | 标记最终版本 | 表达选择最终版本的意图。 |
| `MARK_REVIEW` | 标记待复核 | 表达人工复核标记意图，不复制 Workflow 状态。 |
| `NEW_WELL` | 新井资料 | 表达新井文件输入意图；目前上传沿用已有入口。 |
| `REPLACE_INPUT` | 替换既有输入资料 | 表达同任务输入替换，不等同于目前上传创建新任务。 |
| `ADD_EVIDENCE` | 补充验证证据 | 表达追加岩心、录井、试油、邻井等资料的意图。 |

## 20. TargetType（操作对象类型）

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `WELL` | 井 | 整口井的业务对象。 |
| `RAW_CURVE` | 原始测井曲线 | 原始曲线对象；可识别不授权修改原始数据。 |
| `LITHOLOGY` | 岩性 | 岩性识别结果。 |
| `VSH` | 泥质含量 | 泥质含量结果。 |
| `POROSITY` | 孔隙度 | 孔隙度结果或对应参数目标。 |
| `PERMEABILITY` | 渗透率 | 渗透率结果或对应参数目标。 |
| `WATER_SATURATION` | 含水饱和度 | 含水饱和度结果。 |
| `FLUID` | 流体解释 | 综合流体识别结果。 |
| `ZONE_CLASSIFICATION` | 油气水层分类 | 解释层类型结果。 |
| `INTERVAL` | 解释层段 | 层段对象；具体标识在范围字段中。 |
| `LAYER_BOUNDARY` | 层界 | 解释层段的边界对象。 |
| `RW` | 地层水电阻率 | 地层水电阻率参数。 |
| `ARCHIE_PARAMETER` | Archie 参数 | Archie 公式相关参数，不在此定义取值规则。 |
| `CUTOFF` | 截止值 | 业务判别阈值，不在此补造具体标准。 |
| `MODEL` | 预测模型 | 专业预测模型对象。 |
| `METHOD` | 计算 / 解释方法 | 计算或解释方法对象。 |
| `EVIDENCE` | 验证证据 | 用于验证的外部证据对象。 |
| `REPORT` | 报告 | 解释报告对象。 |
| `EXECUTION` | 解释版本 | 一次实际执行所形成的版本对象。 |

## 21. OperationScopeKind 与 OperationScope（操作范围）

OperationScope 是以 `kind` 判别的 Pydantic 联合类型。Target 与 Scope 分开存储。

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `WHOLE_WELL` | 整井范围 | 无局部范围附加字段。 |
| `INTERVAL` | 单个解释层 | 必须提供稳定 `interval_id`；不接受层号代替标识。 |
| `MULTI_INTERVAL` | 多个解释层 | 必须提供非空 `interval_ids`。 |
| `DEPTH_RANGE` | 深度区间 | 提供 top、bottom 和 depth_reference，要求 top 小于 bottom，单位为米。 |
| `DEPTH_POINT` | 单深度点 | 提供 depth 和 depth_reference，单位为米。 |
| `FILTER_SET` | 条件筛选结果集 | 保存 filter_expression 与 resolved_ids；null 为未解析，空列表为无匹配。 |

筛选不在本阶段执行；未来冻结集合时必须明确所依据的任务和 Execution。

## 22. DepthReference（深度基准）

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `MD` | 测量深度 | 沿井眼测量的深度基准。 |
| `TVD` | 真垂深 | 真垂直深度基准。 |
| `TVDSS` | 海拔基准真垂深 | 海拔基准的真垂直深度，可带符号；本阶段不转换。 |

## 23. ExecutionReferenceKind（解释版本引用）

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `TASK_CURRENT` | 任务当前最新版本 | 指向任务当前版本，尚未执行查询。 |
| `ACTIVE_BASE` | 当前工作基线版本 | 用户继续工作的基线，可能不同于任务最新版本。 |
| `PREVIOUS` | 上一版本 | 同一任务上一版本，具体解析由后续 Resolver 决定。 |
| `LATEST_SUCCESSFUL` | 最近成功版本 | 指定同任务最近成功版本。 |
| `FIRST` | 首次解释版本 | 指定同任务首次解释版本。 |
| `SEQUENCE` | 按版本序号指定 | sequence 必须是正整数，拒绝布尔值、字符串和小数。 |
| `EXECUTION_ID` | 按明确执行 ID 指定 | execution_id 必须非空，后续仍需校验归属。 |

只有序号选择器携带 sequence，只有 ID 选择器携带 execution_id；其他引用不携带二者。
本模型不替换既有 TaskReference，也不查询版本。

## 24. ValueMode（数值修改方式）

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `ABSOLUTE` | 设置成绝对值 | 例如改成 16% 表达为 value=16、unit=%。 |
| `DELTA` | 增加 / 减少绝对量 | 例如提高 2 个百分点表达为 value=2、unit=percentage_point。 |
| `PERCENT_CHANGE` | 按比例变化 | 例如提高 2% 表达为 value=2、unit=%，相对于原值变化。 |

ValueSpec 必须提供 mode、有限数值 value 和非空 unit，不转换单位或推断专业取值范围。
unit 为文本，`1` 表示示例中的无量纲比例；百分数和百分点的换算由后续明确契约负责。

## 25. PersistMode（结果保存意向）

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `PREVIEW` | 预览 / 临时结果 | 不请求创建正式结果版本。 |
| `CREATE_VERSION` | 创建正式新版本 | 表达追加版本的意向，不自动创建 Execution。 |

OperationPlan 要求显式填写保存意向；没有原地覆盖历史的模式。

## 26. OperationConstraintType 与 OperationConstraint（操作限制）

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `ONLY_SCOPE` | 仅限当前范围 | 不超出当前节点或共享上下文明确的范围。 |
| `DO_NOT_PERSIST` | 不保存正式结果 | 表达不创建正式结果的限制。 |
| `EXCLUDE_MODEL` | 排除模型 | 必须携带 model_id。 |
| `USE_RULE_ONLY` | 仅使用规则 | 表达仅采用规则方法的限制。 |
| `KEEP_UNAFFECTED_RESULTS` | 保留未受影响结果 | 表达保留意向，不在此计算影响范围。 |
| `NO_FULL_RERUN` | 禁止整井全量重跑 | 不允许将局部请求自动升级成全量执行。 |
| `EXCLUDE_SCOPE` | 排除指定范围 | 必须携带结构化 scope。 |

限制可组合；仅排除模型和排除范围携带各自参数，冲突裁决留给后续 PlanValidator。

## 27. OperationEdgeType（操作关系）

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `SEQUENCE` | 顺序依赖 | 表达一个操作发生在另一个之后。 |
| `DATA_DEPENDENCY` | 数据依赖 | 表达消费另一个操作的输出。 |
| `COMPARE_DEPENDENCY` | 比较依赖 | 表达比较需要某个操作输出。 |

边使用非空 from_operation_id、to_operation_id 和 type；Schema 不构造专业 DependencyGraph。
Task 10.5-D PlanValidator 校验端点、输入依赖与图环；OperationInputReference 独立表达操作输出或版本输入。

## 28. ResolutionConfidence（解析置信程度）

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `HIGH` | 高置信解析 | 解析方声明信息较明确，不等于执行授权。 |
| `MEDIUM` | 中等置信解析 | 解析方声明仍有不确定性。 |
| `LOW` | 低置信解析 | 解析方声明信息不可靠或不足。 |

## 29. ResolutionOutcome（解析结果）

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `EXECUTABLE` | 可执行 | 未来解析 / 验证结果声明可进入写操作处理。 |
| `READ_ONLY` | 可只读执行 | 未来解析 / 验证结果声明仅执行读取。 |
| `NEED_CLARIFICATION` | 需要澄清 | 信息不唯一或不足，不能猜测。 |
| `KNOWN_UNSUPPORTED` | 已识别但当前不支持 | 属于已知领域操作，当前尚不具备能力资格。 |
| `REJECTED` | 已拒绝 | 请求被明确拒绝。 |

OperationResolution 仅保存 outcome、confidence、plan 和证据等说明。Task 10.5-D 的
PlanValidationResult 复用上述结果枚举，提供整个计划的纯规划裁决，未接入 InteractionDecision。
EXECUTABLE 不是数据库授权，进入 Tool 前仍须通过 B 的引用解析和既有 Application 校验。

## 30. OperationCapabilityStatus（操作能力状态）

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `ENABLED` | 已启用 | 业务已确认且当前版本开放；仅具有进入执行层的资格。 |
| `DISABLED` | 已禁用 | 业务明确禁止或当前不开放；不可执行。 |
| `UNVERIFIED` | 待业务确认 | 需求或业务规则待确认；默认不可执行。 |
| `NOT_IMPLEMENTED` | 尚未实现 | 已有需求或底层能力，但独立操作处理尚未完成；不可执行。 |

OperationCatalog 按 ActionType 查询声明。is_executable 仅判断状态，不校验具体计划，
不检查或调用 handler_name。默认范围、参数和业务限制见
[Task 10.5-A](tasks/010-5a-operation-models.md)。调整开关不改变 OperationPlan Schema。

## 31. ReferenceAccessMode（引用访问用途，Task 10.5-B）

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `READ_ONLY` | 只读引用解析 | 多任务且焦点丢失时，可按旧交互约定选择最近绑定任务。 |
| `WRITE` | 写操作引用解析 | 多任务且焦点丢失时必须报告歧义；解析过程本身仍然只读。 |

这是引用安全策略，不是 InteractionDecision 或 ResolutionOutcome 的替代品。

## 32. TaskResolutionSource（任务解析来源）

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `ACTIVE` | 当前操作焦点 | 使用当前 Session 内有效的 active_task_id。 |
| `ONLY_TASK` | 会话唯一任务 | 焦点失效时，会话仅有一个可用的授权任务。 |
| `LATEST_BOUND` | 最近绑定任务 | 只读解析在多个候选任务中使用最后绑定的任务。 |
| `EXPLICIT_WELL` | 显式井号 | 按指定井号找到唯一匹配任务。 |
| `EXPLICIT_TASK` | 显式任务标识 | 指定 Task ID 已通过当前 Session Binding 核验。 |
| `PREVIOUS_BOUND` | 上一绑定任务 | 在明确 current anchor 之前的相邻绑定任务。 |

同井多任务只读选择最近绑定项时，match_count 记录匹配数；写操作只允许选匹配井的有效焦点。
ResolvedExecutionReference 的 resolution_source 复用第 23 节 ExecutionReferenceKind；
ResolvedScope 的 resolution_source 复用第 21 节 OperationScopeKind，没有新增平行选择器。

## 33. IntervalIdentitySource（版本内层段身份来源）

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `NATIVE` | 原生层段标识 | 原始行已有有效 interval_id，用该值构造带 Execution 限定的身份。 |
| `EXECUTION_ORDINAL` | 版本内顺序派生标识 | 原始行没有 interval_id，用终态快照中从 1 开始的位置派生身份。 |

当前 Fixture 没有原生层段标识。两种身份均绑定 Execution；原生字符串另外保存在 native_interval_id。
生成的 interval_id 不写回历史 State，不是数据库主键，不表示跨版本同一地质层。
运行中快照尚未固定时，不生成此身份。

## 34. Operation Reference Resolver 稳定错误码

| 错误码 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `AMBIGUOUS_TASK_REFERENCE` | 任务引用存在歧义 | 多任务写请求缺少明确焦点，或同井多任务无法唯一选择。 |
| `AMBIGUOUS_EXECUTION_REFERENCE` | 执行版本引用存在歧义 | 候选 Execution 存在重复序号，无法唯一选择。 |
| `INTERVAL_NOT_FOUND` | 当前版本不存在目标层段 | 序号或 ID 无匹配，或 ID 来自另一版本。 |
| `DEPTH_REFERENCE_UNAVAILABLE` | 深度基准不可用 | 当前版本缺少原始深度数据，或请求基准与真实数据不一致。 |
| `DEPTH_OUT_OF_RANGE` | 深度超出数据覆盖 | 点或区间超出当前 Execution 原始数据边界，不静默裁剪。 |
| `FILTER_SET_UNRESOLVED` | 条件筛选集合尚未冻结 | resolved_ids 为 null，不能作为已解析集合使用。 |
| `STALE_CONTEXT_REFERENCE` | 上下文引用失效 | 显式工作基线丢失、不存在或跨任务，或层段快照仍可能变化。 |
| `AMBIGUOUS_SCOPE` | 操作范围无法可靠确定 | 层段行、顶底深度或原生 ID 无效，或原生 ID 重复；不能跳过坏行重新编号。 |

复用 `TASK_NOT_FOUND`（任务不存在或未授权）、`PREVIOUS_TASK_NOT_FOUND`（无上一绑定任务）、
`SESSION_WELL_NOT_FOUND`（会话内无指定井）和 `EXECUTION_NOT_FOUND`（当前任务无指定版本）。
显式 Task / Execution ID 不能越过 Session Binding 或 Task 归属检查；错误不暴露其他会话事实。
Resolver 只抛出项目现有 DataError，不负责映射成澄清对话或执行计划。

## 35. ClarificationSlot（通用澄清槽位，Task 10.5-D）

| 代码值 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `TASK` | 任务对象 | 任务缺失或复合写的任务归属尚未明确。 |
| `EXECUTION` | 版本对象 | 尚需用户明确工作版本。 |
| `SCOPE` | 作用范围 | 需要明确局部操作范围。 |
| `TARGET` | 操作目标 | 操作已知，但目标未明确。 |
| `VALUE` | 修改值 | 结果或参数缺少正式 ValueSpec。 |
| `METHOD` | 方法或模型 | 由动作区分缺 method_id 或 model_id，不增加重复 MODEL 枚举。 |
| `COMPARE_TARGET` | 比较对象 | 需要至少两个明确输入；不自动猜上一版。 |
| `PERSIST_MODE` | 保存方式 | 需要明确预览还是创建正式版本。 |
| `CONFLICT_RESOLUTION` | 冲突选择 | 无法映射到单一字段的约束冲突需用户重新描述；已知任务或版本冲突分别使用 TASK / EXECUTION。 |

ClarificationIssue 同时保存可选 operation_id、slot、error_code、中文 message 和可选 evidence。
通用澄清仍复用 `CLARIFICATION_REPLY`（澄清回复）和 `CORRECTION`（修正上一输入），
没有新增平行意图或执行状态。普通回复只能补对应节点的待缺槽位；修正只操作尚未执行的 Partial Plan。

## 36. Operation Planning 稳定错误码（Task 10.5-D）

| 错误码 | 中文名称 | 中文语义 |
| --- | --- | --- |
| `MULTI_OPERATION_CONFLICT` | 复合操作存在冲突 | 保存/预览约束、禁止全量重跑或已确定全部被排除的限定范围冲突；需澄清整个计划。 |
| `CONDITIONAL_EXECUTION_UNSUPPORTED` | 条件式自动执行当前不支持 | 条件结构可保存和校验，但不求值、不循环、不执行任意节点。 |
| `CROSS_TASK_WRITE_UNSUPPORTED` | 跨任务复合写当前不支持 | 多个写节点指向不同明确 Task，整个计划不执行。 |
| `COMPARE_TARGET_REQUIRED` | 比较对象不足 | 少于两个不同的结构化输入，需补比较对象。 |
| `VIEW_ACTIVE_CONTEXT_CONFLICT` | 查看与操作上下文冲突 | 跨任务双焦点返回 TASK 槽位；同任务历史 View 且无工作基线返回 EXECUTION 槽位，普通澄清可以补齐。 |
| `INVALID_OPERATION_PLAN` | 操作计划结构非法 | 重复节点、无效端点/输入/条件/输出引用、自环或依赖环；也拒绝未经合并的交互回复直接执行。 |
| `CLARIFICATION_SLOT_INVALID` | 澄清槽位修补非法 | 没有有效 Pending、修补节点不明确、越权补槽或普通回复试图修改锁定引用。 |

复用 `CLARIFICATION_REQUIRED`（需要补充澄清）、`UNSUPPORTED_OPERATION`（当前操作能力不支持）
以及 `OUT_OF_DOMAIN`（领域外请求）。能力事实保留第 30 节全部状态；非 ENABLED 状态不合并丢失。
条件式和跨任务复合写返回 KNOWN_UNSUPPORTED（已识别但当前不支持）；缺槽和可澄清冲突返回
NEED_CLARIFICATION（需要澄清）；非法图返回 REJECTED（已拒绝）。
CAPABILITY_QUERY（能力询问）对合法结构始终返回 READ_ONLY（可只读执行），不提供写执行计划。

## 37. Operation Execution Bridge 结果与稳定代码（Task 10.5-E1）

OperationBridgeResult 的 `SUCCESS`（命令成功）仅表示只读命令完成或后台写命令已提交，
不表示 Workflow 已完成；执行终态仍看 TaskCommandResult.execution_status。
能力询问返回 `READ_ONLY`（只读能力说明），不调用业务命令。失败复用
`REJECTED`（已拒绝）、`NEED_CLARIFICATION`（需要澄清）、
`KNOWN_UNSUPPORTED`（已识别但当前不支持），另保留完整 PlanValidationResult。

| 稳定代码 | 中文名称 | 边界 |
| --- | --- | --- |
| `INITIAL_INPUT_ROUTE_REQUIRED` | 首次解释需要输入资料入口 | generic Bridge 不创建空 Task/InputVersion/Execution；已有上传和 Fixture 入口继续支持首次解释。 |
| `OPERATION_SESSION_IDENTITY_REQUIRED` | 操作需要会话身份 | runner 缺少服务端 Session Identity 时关闭执行入口，不用聊天或 Active 冒充授权。 |
| `HISTORICAL_BASE_WRITE_UNSUPPORTED` | 历史基线写入当前不支持 | 写操作选中版本与 Task 当前版本不同，不把历史写转换成当前写。 |
| `COMPOUND_EXECUTION_UNSUPPORTED` | 当前执行桥不支持该复合执行 | 除同任务同版本参数聚合和纯读以外的多节点执行，以及无法消费的数据/比较依赖，均不部分执行。 |
| `STATUS_EXECUTION_SELECTION_UNSUPPORTED` | 状态查询暂不支持历史版本选择 | STATUS 只接受当前版本语义，不忽略明确历史选择。 |
| `UNSUPPORTED_VALUE_MODE` | 当前不支持该值变更模式 | 只支持 ABSOLUTE（绝对值），不自动换算增量或比例变化。 |

复用既有 INVALID_OPERATION_PLAN（操作计划结构非法）、MULTI_OPERATION_CONFLICT（复合操作存在冲突）、
UNSUPPORTED_OPERATION（当前操作能力不支持）、UNSUPPORTED_PARAMETER（当前参数不支持）、
STALE_CONTEXT_REFERENCE（上下文引用失效）、STALE_EXECUTION_PLAN（执行计划已失效）、
TASK_EXECUTION_ACTIVE（任务仍有活跃执行）及 B Resolver / Application 的既有错误。
参数名与目标不一致拒绝；重复参数同值去重、异值整体冲突。并发版本前置条件不提供历史分支功能。


## 38. Operation Tool 交互模式与未解析层号（Task 10.5-E2）

| 代码值 | 中文含义 | 边界 |
| --- | --- | --- |
| `PLAN` | 提交新计划 | 严格 PartialOperationPlan；新请求清除旧 Pending。 |
| `CLARIFICATION_REPLY` | 补齐待澄清计划 | ClarificationPatch；普通补齐只能修改允许槽位。 |
| `CANCEL` | 取消待澄清请求 | 不取消后台 Execution；清 Pending 后零业务副作用。 |
| `SET_ACTIVE_CONTEXT` | 显式切换操作焦点 | WRITE（写安全）引用解析；不创建 Execution，不是业务 Action。 |
| `INTERVAL_ORDINAL` | 单层号引用 | 一基正整数，仅 Partial / 模型输入层允许。 |
| `MULTI_INTERVAL_ORDINAL` | 多层号引用 | 非空正整数，稳定去重，必须全部解析成功。 |

复用 CORRECTION（修正上一输入）、INVALID_OPERATION_PLAN（操作结构非法）及既有错误码。
未新增 Execution、Workflow 或持久化状态。

## 39. InterpretationStage、StageRunStatus 与 StageValidity（Task 11A）

`InterpretationStage`（解释业务阶段）与第 10 节 `ExecutionStage`（执行规划阶段）是同一
枚举对象；`DECODE`（数据准备）是 `DATA_DECODE`（已有数据准备阶段）的别名，协议值不变。
StageRunStatus（阶段执行状态）表达一次实际执行的生命周期，不替代步骤状态、后台执行状态或规划动作。

| 代码值 | 中文含义 |
| --- | --- |
| `PENDING` | 等待执行 |
| `RUNNING` | 执行中 |
| `WAITING_CONFIRM` | 成功产出，等待用户确认 |
| `CONFIRMED` | 已由用户或系统确认，可供下游消费 |
| `FAILED` | 未成功完成，保留错误 |

| StageValidity | 中文含义 |
| --- | --- |
| `CURRENT` | 当前工作版本可用 |
| `STALE` | 当前工作版本已失效；不改变原执行终态，不回写历史 Execution |

## 40. StageImpact（修改影响类型）与阶段稳定错误码

| 修改影响代码值 | 中文含义 | 受影响阶段 |
| --- | --- | --- |
| `DatasetPatch` | 原始 / 标准化曲线局部数据修改 | 预处理、解释、报告 |
| `PreprocessParameterChange` | 预处理参数修改 | 预处理、解释、报告 |
| `InterpretationParameterChange` | 解释参数修改 | 解释、报告 |
| `InterpretationOverride` | 人工解释修改；不代替同名现有参数模型 | 解释、报告 |
| `ReportConfigChange` | 报告配置修改 | 报告 |

| 错误代码值 | 中文含义 |
| --- | --- |
| `INVALID_STAGE_TRANSITION` | 非法阶段状态转换或重复启动 |
| `STAGE_DEPENDENCY_UNAVAILABLE` | 前置结果未确认、失效、缺失或输入引用不匹配 |
| `STAGE_CHANGE_DURING_RUN` | 受影响阶段执行中，不能修改其依赖 |
| `STAGE_NOT_COMPLETED` | 阶段未完整完成、被阻断或需要人工复核 |
| `REPORT_GENERATION_FAILED` | 报告生成异常 |

详细设计见 [四阶段执行模型](architecture/four-stage-execution-model.md)。

## 41. ExecutionRunMode 与阶段确认错误码（Task 11C）

| 代码值 | 中文含义 |
| --- | --- |
| `CONTINUOUS` | 连续执行；W01～W10 和报告一次运行，阶段由系统自动确认。 |
| `STAGED_CONFIRMATION` | 分阶段确认执行；每个阶段成功后释放 Worker 并等待人工确认。 |

| 错误代码值 | 中文含义 |
| --- | --- |
| `INVALID_EXECUTION_MODE` | 当前 Execution 不是分阶段确认模式。 |
| `STAGE_CONFIRMATION_CONFLICT` | 等待阶段、预期 StageRun ID 或确认状态已经变化。 |
| `EXECUTION_NOT_CURRENT` | 请求针对历史 Execution，禁止确认或改写。 |

分阶段模式中，非最终确认把同一个 Execution 从
`WAITING_CONFIRMATION`（等待阶段确认）恢复为 `QUEUED`（排队等待）；只有 REPORT（报告）
确认会进入 `SUCCESS`（执行成功）或 `WARNING`（完成但有告警）。

## 42. StageResultView 确认阻断码（Task 11D）

| 阻断码 | 中文含义 |
| --- | --- |
| `EXECUTION_NOT_CURRENT` | 阶段结果所在的查询 Execution 已不是 Task 当前执行。 |
| `EXECUTION_NOT_WAITING_CONFIRMATION` | Execution 当前不处于等待阶段确认状态。 |
| `STAGE_NOT_WAITING_CONFIRM` | StageRun 当前不处于等待确认状态。 |
| `STAGE_RESULT_STALE` | 当前工作快照中的阶段结果已经失效。 |

详细设计见 [四阶段结果展示契约](architecture/four-stage-result-contract.md)。
