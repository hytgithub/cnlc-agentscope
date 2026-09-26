# 交互状态与集中策略（Current Design，Task 10.3）

状态、枚举、任务引用和稳定错误码的统一中文释义见 [11-status-enum-glossary.md](11-status-enum-glossary.md)。本文保留英文代码值以便和代码、日志、测试一一对应。

## 1. 边界

保持 **ReAct for Interaction，Workflow for Execution**。同一次 qwen-plus ReAct 理解意图，
TaskReference / SessionTaskResolver 解析当前 Session 的授权目标，InteractionPolicy 决定
EXECUTE、QUERY、CLARIFY 或 REJECT。Application 决定版本、并发、依赖与执行计划，
Workflow 保持 W01–W10。无第二次 IntentClassifier 调用、数据库表或 migration。

五个业务任务动作不变。新增 `request_interpretation_clarification` 是纯交互工具：仅保存
完整待澄清修改或返回能力边界，不创建 Task / InputVersion / Execution，不调用专业工具。

## 2. Derived Snapshot 与 Phase

`interaction_state.py` 的 InteractionSnapshot 通过 SessionTaskResolver、授权 Binding、
Task、当前 Execution 及 AgentScope Session middle_context 动态派生。字段包括
session_has_task、active_task_id、active_well_id、current_execution_id、execution_status、
execution_sequence、current_step、report_ready、has_previous_execution、has_previous_task、
pending_clarification 和 phase。每次模型推理重新读仓库；快照不作为业务事实持久化。

| Phase | 中文名称 | 含义 |
| --- | --- | --- |
| `NO_TASK` | 当前无任务 | Session 无绑定任务 |
| `ACTIVE` | 当前有活跃执行 | Execution 为 `QUEUED`（排队等待）/ `RUNNING`（正在执行），优先于澄清 |
| `NEED_CLARIFICATION` | 等待用户澄清 | 非活跃任务有完整、有效的 PendingClarification |
| `READY` | 可接受新操作 | 当前任务可以进入 Application 写操作校验 |

`SUCCESS`（执行成功）/ `WARNING`（完成但有告警）/ `FAILED`（执行失败）/ `BLOCKED`（被阻断）/ `REVIEW_REQUIRED`（需要人工复核）仍然是 ExecutionStatus。
READY 不承诺某次重跑一定可行，InputVersion、有效来源和并发仍由 Application 校验。

## 3. Policy 与工具 Contract

InteractionPolicy 输出：`ALLOW`（允许执行，对应 EXECUTE）、`READ_ONLY`（只读查询，对应 QUERY）、`CLARIFY`（需要澄清）和 `REJECT`（拒绝本次操作）。无任务操作返回 TASK_NOT_FOUND，运行中写操作返回含版本和步骤的
TASK_EXECUTION_ACTIVE。CURRENT 无报告返回 REPORT_NOT_READY；PREVIOUS、LATEST_SUCCESSFUL
或显式 execution_id 由既有报告应用接口严格选版。

模型可见 Schema 仅使用 task_reference；旧 Python adapter 的 task_id 调用仍可兼容，
但不能同时提供二者。`CURRENT`（当前井）/ `PREVIOUS_TASK`（上一口井）的 value 应省略或为 null；`WELL_ID`（按井号指定）/ `TASK_ID`（按可信任务号指定）需要非空值。指定井查询失败不会切换焦点。成功业务操作才更新 active task。

纯交互工具 Contract：

| 项目 | 定义 |
| --- | --- |
| Name | request_interpretation_clarification |
| Responsibility | 保存修改缺参数的交互状态，或返回冲突／能力限制 |
| Input | reason 枚举；PARAMETER_NAME 需要 known_value；可选 task_reference |
| Output | error_code、固定安全 message；不含业务执行结果 |
| Errors | CLARIFICATION_REQUIRED、UNSUPPORTED_OPERATION、UNSUPPORTED_PARAMETER、TASK_NOT_FOUND 等 |
| Timeout | 仅现有 repository/runtime I/O 超时，无额外网络或专业执行 |
| Status | 交互工具以 ERROR 返回需要处理的语义；不产生 ExecutionStatus |
| Mock | MockTaskShellModel 在同一 ReAct 中形成相同 Schema 调用 |
| Test | 策略矩阵、真实 AgentScope/Mock ReAct 集成与真实 PG/Redis Binding 恢复 |

模型一次输出多个写 Tool，或同时输出写 Tool 与澄清 Tool 时，middleware 在任何执行前
拒绝整批。一个 MODIFY 可以同时改变 POR/PERM，并由原 Streamer 返回报告。
没有把“修改后给报告”拆成两个业务执行。

## 4. PendingClarification 生命周期

PendingClarification 仅保存在 `AgentState.middle_context.cnlc_pending_clarification`。
包含 operation=MODIFY、known_value、missing=PARAMETER_NAME、原始 TaskReference、已授权
固定 task_id、服务端 owner_token、created_turn、expires_at。对象一次完整写入。

- 只允许紧邻下一轮补齐，默认最长 10 分钟（CNLC_CLARIFICATION_TTL_SECONDS）；不是跨轮工作队列。
- 下一轮只给参数名时，modify 工具的 parameter_name 从有效 pending 提取值并固定目标。
- 成功补齐、完整新操作、无关回复、取消观察、能力拒绝、过期或格式不完整时清除。
- 本轮新建 pending 不在本轮回复结束时清除。
- Context compression 不依赖聊天摘要，完整 middle_context 保持有效。
- 新 runner 无法证明旧 pending 的请求生命周期，安全丢弃，即使 Redis 恢复该对象。
- active_task_id 可继续由 Session State 恢复；不可恢复则 CURRENT fallback 到最近 Binding。

澄清、稳定拒绝、STATUS、GET_REPORT 直接使用 Tool 事实生成回复，不再调用模型改写或重试。
START / MODIFY / FULL_RERUN 成功创建 Execution 后仍由 ExecutionReplyStreamer 展示真实过程。
SSE 断开仍不取消后台 Worker。

## 5. Scenario Matrix

| Current State | User Action | Expected Decision | Tool | Create Execution? | Expected Response / Error |
| --- | --- | --- | --- | --- | --- |
| NO_TASK | STATUS | REJECT | get_interpretation_status | 否 | TASK_NOT_FOUND，请先上传 |
| NO_TASK | GET_REPORT | REJECT | get_interpretation_report | 否 | TASK_NOT_FOUND |
| NO_TASK | MODIFY | REJECT | modify_well_interpretation | 否 | TASK_NOT_FOUND |
| NO_TASK | FULL_RERUN | REJECT | rerun_well_interpretation | 否 | TASK_NOT_FOUND |
| NO_TASK / READY | START | EXECUTE | run_well_interpretation | 是 | 新 Task/Input/Execution，沿用上传行为 |
| READY | MODIFY | EXECUTE | modify_well_interpretation | 是 | Application 规划 RUN/REUSE，流式报告 |
| READY | NO_EFFECTIVE_CHANGE | REJECT | modify_well_interpretation | 否 | 参数已经是目标值，本次没有新执行 |
| READY | FULL_RERUN | EXECUTE | rerun_well_interpretation | 是 | W01 起全量执行，保留参数 |
| READY | STATUS | QUERY | get_interpretation_status | 否 | 本轮重新读取持久事实 |
| READY | PREVIOUS_REPORT | QUERY / REJECT | get_interpretation_report | 否 | 同 Task 前一版；不存在 REPORT_NOT_FOUND |
| ACTIVE | MODIFY | REJECT | modify_well_interpretation | 否 | TASK_EXECUTION_ACTIVE，版本与步骤 |
| ACTIVE | FULL_RERUN | REJECT | rerun_well_interpretation | 否 | TASK_EXECUTION_ACTIVE |
| ACTIVE | STATUS | QUERY | get_interpretation_status | 否 | 同 Execution 当前持久步骤 |
| ACTIVE | CURRENT_REPORT | REJECT | get_interpretation_report | 否 | REPORT_NOT_READY，不用旧报告冒充 |
| ACTIVE | PREVIOUS_REPORT | QUERY / REJECT | get_interpretation_report | 否 | 严格同 Task 前一版 |
| FAILED | STATUS / 为什么失败 | QUERY | get_interpretation_status | 否 | execution_id、error_code、failed/current step，无原始异常 |
| FAILED | REPORT | QUERY / REJECT | get_interpretation_report | 否 | 本 Execution 诊断报告；若无则 REPORT_NOT_READY |
| FAILED | FULL_RERUN | EXECUTE / REJECT | rerun_well_interpretation | 由 Application 决定 | 有 InputVersion 可重建；缺少条件返回安全错误 |
| BLOCKED | STATUS / 缺什么 | QUERY | get_interpretation_status | 否 | missing_data、affected_step、停止步骤 |
| BLOCKED | GET_REPORT | QUERY / REJECT | get_interpretation_report | 否 | 本版诊断报告或 REPORT_NOT_READY |
| REVIEW_REQUIRED | STATUS | QUERY | get_interpretation_status | 否 | 明确需要人工复核，不自动继续 |
| WARNING | REPORT | QUERY | get_interpretation_report | 否 | 本版已完成报告 |
| WARNING | MODIFY | EXECUTE | modify_well_interpretation | 是 | 允许后续写操作，保留应用校验 |
| MULTI_WELL | CURRENT | QUERY / EXECUTE | 对应业务 Tool | 写动作才可能 | active task，不使用最近 ToolResult |
| MULTI_WELL | PREVIOUS_TASK | QUERY / EXECUTE | 对应业务 Tool | 写动作才可能 | 上一口井；成功后成为 active |
| MULTI_WELL | WELL_ID | QUERY / EXECUTE | 对应业务 Tool | 写动作才可能 | Session 中该井最近 Task |
| MULTI_WELL | UNKNOWN_WELL | REJECT | 对应业务 Tool | 否 | SESSION_WELL_NOT_FOUND，不切 CURRENT |
| 单 Task | PREVIOUS_TASK | REJECT | 对应业务 Tool | 否 | PREVIOUS_TASK_NOT_FOUND |
| READY | 改成0.16 | CLARIFY | request_interpretation_clarification | 否 | 保存 pending，询问参数名 |
| NEED_CLARIFICATION | parameter completion | EXECUTE | modify_well_interpretation(parameter_name) | 是 | 从 pending 补值，成功后清除 |
| NEED_CLARIFICATION | unrelated operation | QUERY / REJECT / EXECUTE | 对应 Tool 或无 Tool | 按完整新动作 | 旧 pending 清除，无关参数名不能使用旧值 |
| UNSUPPORTED_DOMAIN_OPERATION | SW-only / 岩性 / 层段重算 | REJECT | 纯交互 Tool | 否 | UNSUPPORTED_OPERATION，仍属于测井领域 |
| UNSUPPORTED_PARAMETER | Rw / Archie / Sw override | REJECT | 纯交互 Tool | 否 | UNSUPPORTED_PARAMETER |
| 多操作冲突 | MODIFY + FULL_RERUN / 不支持的比较 | CLARIFY | 纯交互 Tool / 整批保护 | 否 | 明确支持与不支持部分，本次未执行 |
| DOMAIN_QUERY | 层段原因 / 证据 / 概念问答 | REJECT | 纯交互 Tool | 否 | 可提供报告，受控专业问答和证据查询尚未开放 |
| OUT_OF_DOMAIN | 天气 / Java / 笑话 | REJECT | 无 | 否 | 单井常规测井解释领域边界 |
| CONTEXT_COMPRESSION | CURRENT / PREVIOUS_TASK / pending completion | 按事实裁决 | 同上 | 按动作 | active 与完整 pending 在 Session State；不从摘要补造 |
| BACKEND_RESTART | CURRENT / PREVIOUS_TASK / WELL_ID | 按事实裁决 | 同上 | 按动作 | PG Binding 恢复，pending 安全丢弃 |
| BROWSER_REFRESH | 查询 / 历史报告 | QUERY | 同上 / Read API | 否 | 既有 Session State + PG 恢复，无 SSE replay |

## 6. 诊断与能力限制

STATUS 新增 failed_step、missing_data（仅 field 名称、importance、affected_step）、warning_count、
review_required，错误仅返回稳定 code，不返回 ErrorDetail.message、原始 exception、URL 或连接配置。
`WARNING`（完成但有告警）明确为已完成；`REVIEW_REQUIRED`（需要人工复核）不自动改写结论。W09 的 `INSUFFICIENT_EVIDENCE`（证据不足）属于 ValidationStatus：表示验证证据不够，并不等于解释结论已被证明错误；当前会进入人工复核。

细层段证据、QUERY_RESULT、COMPARE、恢复/暂停/取消、同 Task InputVersion 替换、WPLM/GDSX
主链接入均未实现。上传同井新资料目前仍创建新 Task，不承诺覆盖原 Task 的输入版本。
fixture / company_mock 的业务调用、批次来源与 DERIVED ToolRun 不变。

领域外判断和语义识别仍由 ReAct 完成；Policy 不是自然语言分类器。
无法保证任意模型、任意措辞的意图准确率，生产模型变更应重跑异常交互评估。
