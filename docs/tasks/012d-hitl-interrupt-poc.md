# Task 012D｜AgentScope HITL / Interrupt 阶段确认 A/B POC

> 父任务：Task 012
> 分支：`codex/task-12-agentscope-native-capability-poc`
> 类型：AgentScope 2.0.8 原生交互能力对照 / 阶段确认边界
> 日期：2026-10-06
> 推荐执行模型：GPT-5.6 Sol High
> 原则：只验证 HITL / Interrupt 是否能简化“确认并继续”的交互外壳；StageRun、StageOrchestrator、Execution、版本与真实业务状态仍由项目确定性代码负责。

## 1. 背景

Task 012A～012C.2 已形成阶段性结论：Agent + Toolkit = PARTIAL；Skill = NO_CLEAR_VALUE / VERIFY；Conversation Context = WRAP；Context Compression = WRAP；Authority / Resolver / Validator / Policy / Domain State = KEEP；AgentScope Task Tools = REGISTERED_BUT_UNUSED。

当前下一项待验证能力是 AgentScope 2.0.8 的 HITL / Interrupt。

项目已有四阶段业务执行：

```text
数据解编 → 数据预处理 → 智能处理 → 报告生成
```

现有生产实现已经有 StageRun / StageOrchestrator 以及 WAITING_CONFIRM / CONFIRMED 等阶段业务状态；用户确认通过现有 Operation / CONFIRM_STAGE 路径进入应用层。

本 Task 不讨论“到底哪些阶段必须强制确认”这一业务规则。Q05 仍未最终确认，因此实验使用固定测试 checkpoint 配置，只回答：

> **AgentScope 原生 HITL / Interrupt 能否承担“向用户索要确认、接收确认/拒绝、恢复 Agent 交互”这一层职责，并减少自定义外围交互代码，同时不替代或绕过 StageRun / StageOrchestrator 的业务事实？**

## 2. 必须区分两类状态

### 2.1 Interaction Pause

候选由 AgentScope 原生 HITL / Interrupt 负责：向 UI 发出需要确认事件、接收确认/拒绝/中断结果、恢复 Agent 当前交互、展示确认原因。

### 2.2 Business Stage State

必须继续由项目确定性代码负责：Task / Execution / Stage、StageRun、WAITING_CONFIRM / CONFIRMED / FAILED、阶段结果持久化、重复确认、并发、stale execution、用户切井切版本、外部 API 是否仍运行。

**AgentScope HITL event 不是业务确认事实源。**

## 3. 开始前必须核实 AgentScope 2.0.8 实际 API

必须在当前项目 `.venv` 中核验，不得仅依据文档名称。至少确认：

- RequireUserConfirm / UserConfirmResult 的真实类、event type、字段；
- UserInterrupt 的真实类、event type、字段；
- RequireExternalExecution / ExternalExecutionResult 如有关只记录，不强行引入；
- 事件由哪个 Agent / Tool / middleware 产生；
- reply_stream 如何暂停、结束或等待输入；
- 用户确认结果如何重新注入；
- AgentState / Session 恢复后是否仍保留待 HITL 状态；
- Web UI / server 当前是否已有原生事件透传支持；
- duplicate result / invalid event id 的框架行为；
- HITL 是否要求同一进程、同一 reply 对象，还是可跨请求恢复。

把核验结果写进 Evidence。不得自己模拟一套名字相同的事件后宣称原生 HITL 已验证。

## 4. A/B 设计

使用同一个 Mock Task / Execution / StageOrchestrator / StageRun Fixture。

### A：current_confirm_stage

```text
StageOrchestrator 到达确认点
→ StageRun = WAITING_CONFIRM
→ 当前 Operation / CONFIRM_STAGE 路径
→ Application 校验 Task / Execution / Stage
→ StageRun = CONFIRMED
→ 下一阶段
```

### B：native_hitl_wrapper

```text
StageOrchestrator 到达确认点
→ StageRun = WAITING_CONFIRM
→ 发出 AgentScope 原生 RequireUserConfirm
→ 用户 UserConfirmResult / Interrupt
→ 项目 Adapter 重新读取权威 Task / Execution / Stage
→ Application 执行 confirm / reject
→ StageRun 事实更新
→ Agent 继续
```

B 组禁止：

- 用 AgentState event 状态代替 StageRun；
- confirm=true 后直接调用下一阶段而不经过业务 confirm；
- 把 Task ID / Execution ID / Stage 仅存在 event 文本中；
- 为实验再写第二套 WAITING_CONFIRM 状态机。

## 5. 固定测试 checkpoint

由于 Q05 尚未业务定稿，本 POC 不把测试 checkpoint 写成产品规则。

Fixture 可固定 DATA_DECODE 完成后和 PREPROCESS 完成后两个 checkpoint；REPORT 是否需要确认只作为单独语义测试，不写成默认产品行为。

## 6. 必测场景

### H01｜正常到达确认点

StageRun 必须先持久为 WAITING_CONFIRM；确认前下一阶段 0 次启动。B 组必须真实发出 AgentScope RequireUserConfirm event。

### H02｜确认并继续

确认时重新读取当前 Task / Execution / Stage；只有权威状态仍为 WAITING_CONFIRM 才允许确认；下一阶段只能启动一次。

### H03｜拒绝 / 不确认

拒绝后不得进入下一阶段，已完成阶段结果保留；不得把“停止 Agent 后续推进”描述成“第三方 API 已取消”。

### H04｜重复确认

同一确认提交两次，第二次不得重复启动下一阶段；业务幂等仍由 StageRun / Application 保障。

### H05｜旧确认 / stale 目标

用户界面持有旧确认事件，但 Active 已切换或 Execution 已推进。必须重新校验，旧 event 不得确认别的 Task / Execution / Stage。

### H06｜浏览器刷新 / 新 HTTP 请求后确认

进入 WAITING_CONFIRM 后模拟刷新或 reply 流结束，再从新请求确认。必须回答原生 HITL 是否天然跨请求恢复；若不能，项目应继续以持久化 StageRun 为事实，新请求重新构造交互，而不是把等待状态只留内存。

这是 KEEP / WRAP / NO_CLEAR_VALUE 的关键场景。

### H07｜等待确认时改变目标

用户不确认，而是“先看看刚才的数据质量结果”或“先解释另一口井”。不能误当确认；原 WAITING_CONFIRM 可追踪；切任务后旧确认不得作用到新任务。

### H08｜外部执行与 UserInterrupt 边界

第三方重 API 已经开始时用户说停止。必须明确：

```text
停止 Agent 后续调用 ≠ 第三方任务已经取消
```

如外部 API 不支持取消，只能隔离后续推进和迟到结果，不得把 UserInterrupt 宣称为真实 cancellation。

### H09｜REPORT 确认语义

只核查：已有报告读取是否不需要 HITL；未来正式报告生成若是写操作是否可能独立确认。证据不足时标记 BUSINESS_CONFIRMATION_REQUIRED。不能因四阶段包含 REPORT 就默认四次强制确认。

## 7. 独立指标

分别记录：Native HITL Availability、Event Usage、Pause Correctness、Resume Correctness、Business Authority Binding、Duplicate Safety、Stale Safety、Refresh Recovery、Interrupt Semantics、UI/Event Fit、Code Complexity。

不得用“体验更好”替代业务安全指标。

## 8. 判定规则

最终给出：KEEP / WRAP / NO_CLEAR_VALUE / VERIFY / UNSUITABLE_FOR_CROSS_REQUEST_CONFIRMATION。

可考虑 WRAP 的最低条件：原生 event 真实使用；H01/H02 成立；不绕过 StageRun；H04/H05 不降低安全；H06 有明确跨请求策略；H08 不误报外部取消；确实减少外围事件/UI适配，而不是形成双轨状态。

若原生 HITL 只适合同一 reply 内等待、必须维护第二套 pending 才能跨请求、或集成成本高于现有 CONFIRM_STAGE，则优先 NO_CLEAR_VALUE 或仅局部 WRAP。

## 9. 生产代码边界

本 Task 默认先做隔离 POC / Adapter，不直接删除当前 CONFIRM_STAGE。必须保留 StageOrchestrator、StageRun、Task / Execution / Version、InteractionPolicy、Authority Resolver、duplicate/stale/ownership 校验和真实 API 状态。

## 10. 测试要求

至少建立 H01～H09 自动化证据。建议新增：

- `tests/experiments/test_hitl_interrupt_poc.py`
- 如需要：`experiments/agentscope_native_poc/hitl_runner.py`

必须运行：

```bash
pytest tests/experiments -q
pytest tests/unit -q
pytest tests/integration/test_task_react.py -q
pytest tests/integration/test_interaction_robustness.py -q
git diff --check
```

本 Task 修改 Python 文件 Ruff 必须通过。完整 integration 如受 PostgreSQL / Redis 环境影响，必须区分环境问题与行为失败。

## 11. 文档同步

必须新增：`docs/evidence/10-Task012D-HITL-Interrupt对照实验结果.md`。

并检查/更新：

- `docs/design/04-测井解释智能体技术方案.md`
- `docs/design/05-测井解释智能体测试与验收方案.md`
- `docs/08-intent-and-interaction-design.md`（仅当前实现变化时）
- `docs/10-interaction-state-machine.md`（仅当前实现变化时）
- `docs/09-streaming-progress-and-ui-design.md`（如事件影响 SSE/UI）
- `docs/tasks/012-agentscope-native-capability-poc.md`
- `docs/README.md`
- `docs/evidence/README.md`

若新增稳定状态/错误码，再同步 `docs/11-status-enum-glossary.md`。设计 01～03 只有需求/目标/效果变化时才改。

## 12. 关键业务未知必须保留

不得擅自定稿：Q05 哪些阶段强制确认；REPORT 是否强制确认；拒绝继续后的最终产品状态；第三方真实 API 是否支持取消。

## 13. 完成报告

至少报告：AgentScope 2.0.8 HITL/Interrupt 实际 API 与源码路径、A/B 架构、H01～H09、原生 event 使用次数、Pause/Resume/Duplicate/Stale、Refresh Recovery、Interrupt 与外部取消边界、UI/SSE 适配、代码复杂度、最终判定、Architecture Issue、Documentation Impact、tests/Ruff/diff-check、branch/commit/push。

## 14. 完成后停止

完成 012D 后停止。不要自动开始 TracingMiddleware、04 v1.0 定稿、真实业务 B01～B08，也不要删除现有 CONFIRM_STAGE。先人工评审 HITL 证据。