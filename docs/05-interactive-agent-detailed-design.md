# 交互式测井解释智能体详细设计与 E2E 基线

## 1. 设计目标

交互式入口允许用户首次上传、修改受支持参数、全量重跑、查询状态和读取当前或历史报告。每次业务执行有独立版本和审计轨迹；自然语言只选择任务动作，W01～W10 顺序和局部复用由确定性代码控制。

相关权威设计：

- 数据库实体、字段、并发和 migration：[07-database-design.md](07-database-design.md)
- 意图、参数和任务级 Tool：[08-intent-and-interaction-design.md](08-intent-and-interaction-design.md)
- 首次上传 SSE 与 UI：[09-streaming-progress-and-ui-design.md](09-streaming-progress-and-ui-design.md)
- 运行时持久化：[05-persistence.md](05-persistence.md)

本文只说明对象职责、执行规划和 E2E 约束，不重复上述字段表和事件表。

## 2. 核心对象

### 2.1 InterpretationTask

一口井的持续任务。保存井标识、当前 Execution、最近成功 Execution、当前 InputVersion，以及兼容当前视图。Task 本身不代表某次执行结果。

### 2.2 InterpretationInputVersion

一份通过 Schema 校验的规范化输入快照。UPLOAD 和 FIXTURE 使用同一 Contract，SHA-256 用于判断内容是否改变。Worker 从 payload 重新物化临时输入，不依赖上传路径。

### 2.3 Execution

一次首轮、局部或全量解释版本。包含输入引用、有效 Override、执行起点、成功来源、规划原因、`InterpretationState` 快照、租约、终态和 Markdown。任何重跑都新增 Execution，不覆盖历史。

### 2.4 ExecutionPlan 与四阶段视图

`DependencyResolver` 生成只读 `ExecutionPlan`，四个阶段依次为 DATA_DECODE、PREPROCESS、INTERPRET、REPORT，每个动作是 RUN 或 REUSE，且 REUSE 只能构成连续前缀。执行边界会重新校验 `expected_current_execution_id` 和来源输入，防止过期计划落库。

四阶段是 **Planner View / Read Model**。当前没有独立 StageRun 表；W01～W10 的 `StepExecution` 和 `reused_steps` 才是细节事实。

### 2.5 ToolRun

ToolRun 已实现并由 migration `0004` 持久化。Workflow 在专业 Tool 开始时写 RUNNING，在结果或异常时写终态、输入输出快照、来源和安全错误信息。ToolRun 属于 Execution，UI 可按历史版本读取。

### 2.6 InterpretationOverride

只包含 `sampling_interval`、`por`、`perm`、`prediction_model`。每个 Execution 保存有效快照，不改写 InputVersion。新字段必须先加入依赖影响表；缺少映射时明确报错，不能默认从某一步重跑。

### 2.7 SessionTaskBinding

Binding 已由 migration `0006` 实现。完整 `(user_id, agent_id, session_id)` 决定 Task ownership；`observed_task_ids` 只是会话 runner 的加速缓存。Backend 重启后从 PostgreSQL 恢复绑定，旧 Session 可继续查询和修改原 Task。

一个 Session 可以绑定多个 Task，每个 Task 仍只对应一口井的持续工作对象。
`SessionTaskResolver` 使用 Binding 列表和 `get_task()` 构建内部 Task Summary，
以确定性规则解析 CURRENT、PREVIOUS_TASK、WELL_ID 和 TASK_ID。同井存在
多个 Task 时选绑定顺序中最近创建的 Task。

`active_task_id` 位于 AgentScope Session State，不是测井业务结果。上传新井或
明确访问某井后更新 active task。Backend 重启后优先使用已恢复的
Session State；如果该值不可用，则从 Binding 稳定顺序选最近创建的 Task。
报告 `PREVIOUS` 始终限于 active task 内的前一个 Execution，绝不跨 Task。

## 3. 规划与复用

最近成功来源必须同时满足：Execution 为 SUCCESS/WARNING、有报告、引用的 InputVersion 属于同一 Task 且一致。当前执行用于确定用户正在修改的有效参数，最近成功执行用于确定哪些结果可靠可复用。

| 请求 | 规划结果 |
| --- | --- |
| 首次解释 | W01～W10 RUN，报告 RUN |
| 新输入内容摘要不同 | W01～W10 RUN，报告 RUN |
| 修改采样间隔 | W01 REUSE；W02～W10 RUN；报告 RUN |
| 修改 POR / PERM / prediction_model | W01～W03 REUSE；W04～W10 RUN；报告 RUN |
| 显式全量重跑 | 所有步骤 RUN，并继承当前有效参数 |
| 参数改回可靠来源值且无需重算 | W01～W10 结果复用，只生成新报告 |

复用时，新 `InterpretationState` 显式记录 `ReusedStep` 和 source execution；不会把旧 Execution 直接改成当前版本。当前不支持任意步骤起点、SW-only 重算或暂停/恢复。

## 4. 执行生命周期

1. 应用服务创建 `QUEUED` Execution，并原子更新 Task 当前指针。
2. in-process dispatcher 立即返回给命令调用者并启动 Worker。
3. Worker claim 后进入 RUNNING，持有可续租 lease。
4. MainAgent 根据 `start_step` 和复用快照启动现有 InterpretationWorkflow。
5. Workflow 按 W01～W10 固定顺序执行或跳过已复用前缀，写状态和 ToolRun。
6. Workflow 终态允许时生成报告，并写本 Execution 的 Markdown。
7. Worker 用同一租约身份写 SUCCESS、WARNING、FAILED、BLOCKED 或 REVIEW_REQUIRED。

ExecutionStatus、StepStatus 和 ToolRunStatus 分离。状态 API 查询 PostgreSQL 事实，不从聊天内容、LLM 回复或 dispatcher 内存推断。

## 5. AgentScope 交互边界

`LoggingInterpretationDemoAgent` 是 AgentScope ReAct 交互 Agent，真实模式使用 qwen-plus。业务 `MainAgent` 是 Planner + Orchestrator。ReAct 使用五个业务任务级 Tool 与一个不执行专业业务的纯交互澄清 Tool；MainAgent、InterpretationAgent、ValidationAgent 的职责和 W01～W10 不因 Web 交互改变。

附件首轮由 `UploadInterpretationReply` 确定性处理；纯文本才进入 ReAct。Command 的 Pydantic Schema、Session ownership 和 Resolver 共同构成可信边界。`MockTaskShellModel._command()` 只用于无公网模型的本地联调。

## 6. 后台执行与流式回复

任务提交和任务完成是两个时点。任务级 Tool 的 `ToolResultEnd` 可以在 QUEUED 时返回；Read API 和面板可立即取得 Task/Execution 标识。首次附件回复会继续等待本进程 Worker，并把真实 Telemetry 投影到 ThinkingBlock，随后读取本 Execution 的 Markdown 到 TextBlock，最后发送 ReplyEnd。

等待使用 `asyncio.shield`，SSE 断开不会取消 Worker。后台状态、租约和报告都落 PostgreSQL；重新打开页面由 Binding 和 Read API 恢复。当前没有事件日志表或 SSE replay。

## 7. Read Model 与 Web

Task Read API 返回当前 Execution 和历史摘要；Execution Read API 返回指定历史版本详情。展示模型包括：

- Execution sequence、生命周期、规划原因、起点和来源；
- 四阶段 RUN/REUSE 聚合；
- W01～W10 运行/复用状态；
- 有效 Override 和 InputVersion；
- ToolRun 列表和统计；
- 本 Execution 报告。

用户停留在历史版本时，后台轮询不会强制跳回当前。前端轮询只读 API，终态停止；查询状态的自然语言请求则经 ReAct 调用任务级 Tool。

## 8. E2E 验收基线

### CASE-01 首次解释

- 上传一份合法 JSON 并输入解释请求。
- 创建 Task、InputVersion、Execution #1 和 SessionTaskBinding。
- ToolResultEnd 先提供 QUEUED 标识；Thinking 持续展示真实 W01～W10、六个 Tool 和报告事件。
- Execution 达到 SUCCESS 或 WARNING 后，TextBlock 返回 Execution #1 的原始 Markdown，ReplyEnd 最后发送。

### CASE-02 修改采样间隔

- ReAct 调用 `modify_well_interpretation`，Pydantic 验证正浮点值。
- 创建新 Execution；W01 REUSED，从 W02 继续；历史报告不变。

### CASE-03 修改 POR / PERM

- 百分数转成 0～1 比例；含义不清的裸值不猜测。
- 创建新 Execution；W01～W03 REUSED，从 W04 继续；新报告固化新参数。

### CASE-04 更换预测模型

- 修改的是专业 `prediction_model`，不是 qwen-plus。
- 当前 Mock Provider 可记录该参数并从 W04 重跑；Heavy API 仍未接入。

### CASE-05 全量重跑

- 创建新 Execution，从 W01 运行，继承当前有效 Override。
- 所有步骤重新执行，历史版本可查询。

### CASE-06 状态与历史报告

- 状态命令返回当前持久 Execution、步骤和 Tool 统计。
- 报告命令支持 CURRENT、PREVIOUS、LATEST_SUCCESSFUL 或经归属验证的 execution_id。

### CASE-07 Backend 重启

- 新进程的 runner 缓存为空。
- 旧 Session 通过 SessionTaskBinding 恢复 Task ownership。
- Read API 显示历史 Execution、ToolRun 和报告；后续修改可创建连续的新 Execution。

### CASE-08 断开首次 SSE

- 客户端取得 QUEUED 后断开。
- Worker 继续运行并写终态，重新打开页面通过 Read API 看到最终事实。
- 不要求补播已错过的 Thinking delta。

## 9. Mock 与真实能力边界

Mock Tool 必须实现正式 Contract、Schema、状态和 ToolRun，报告必须明确 Demo/Mock。真实模型意图已可用 qwen-plus，但专业 Heavy Prediction API、LAS/GDSX 正式接入、真实 Report API、独立 Artifact、曲线可视化、步骤级依赖图和 SW-only 重跑仍是 Future。

不得用 LLM 补造孔隙度、渗透率、Sw、有效厚度阈值或专业公式。正式井数据 Schema 尚未冻结：Pending final well-data schema。

## 10. Interaction State 与 Policy（Task 10.3）

新增 InteractionPhase、InteractionSnapshot、PendingClarification、InteractionDecision 与
InteractionPolicy。它们只控制任务操作可用性，业务状态仍由 PostgreSQL Execution 保存。
SessionTaskResolver 继续只负责引用与授权；InteractionStateMiddleware 管理请求轮次、
动态快照与直接事实回复，并保护模型同轮提出的冲突写操作。

澄清对象存 AgentScope middle_context，一次完整写入，只允许下一轮补齐，最长 10 分钟。
压缩不依赖摘要；Backend 新 runner 安全丢弃旧 pending，active task 可恢复或 fallback 到最近 Binding。
START/MODIFY/FULL_RERUN 允许后沿用 ExecutionReplyStreamer；拒绝不创建新 Execution。
STATUS 新增安全诊断：failed_step、missing_data、warning_count、review_required，无原始异常信息。
完整矩阵与恢复限制见 [10-interaction-state-machine.md](10-interaction-state-machine.md)。
