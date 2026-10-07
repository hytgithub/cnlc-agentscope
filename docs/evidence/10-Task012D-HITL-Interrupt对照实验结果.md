# Task 012D｜HITL / Interrupt 阶段确认 A/B 实验结果

> 日期：2026-10-07（Asia/Shanghai）
> 基线：`a8d8c3f`，分支 `codex/task-12-agentscope-native-capability-poc`
> 范围：隔离 POC，离线脚本模型；真实 AgentScope 2.0.8 Agent / Toolkit / Permission / Event，复用项目现有 Mock 业务链。

## 1. 结论与证据边界

**业务确认继续 `KEEP（保留）`；当前生产阶段确认接入原生 HITL 的简化收益为 `NO_CLEAR_VALUE（没有明确收益）`。原生 Tool 交互暂停具有局部 `WRAP（包装）` 候选价值，生产接入保持 `VERIFY（待验证）`。**

H01～H09 共 28 个参数化测试通过，实际发出 19 次 `REQUIRE_USER_CONFIRM（要求用户确认）`、1 次 `REQUIRE_EXTERNAL_EXECUTION（要求外部执行）`。原始事件逐案保存在 `artifacts/native_poc/task012d_hitl_events.jsonl`；JUnit 结果在 `task012d_hitl_tests.xml`；事件计数、版本与本地上游源码 SHA256 在 `task012d_hitl_summary.json`。

不能判定 `UNSUITABLE_FOR_CROSS_REQUEST_CONFIRMATION（不适合跨请求确认）`：AgentState 的 JSON 包含正在等待的 Tool Call，恢复到新的 Agent 后可用原事件确认；无需同一个 Agent/reply Python 对象。但是独立新请求如果没有加载原 AgentState，就不能消费旧确认事件，必须读取持久化 StageRun 后重新构造交互。本实验只验证新 `reply_stream()` 调用和序列化恢复，不声称已验证真实浏览器刷新、HTTP 传输或 PostgreSQL/Redis 跨进程恢复。

没有改生产代码、SSE 或前端，没有移除 CONFIRM_STAGE；不把 POC 作为迁移授权。没有评估真实模型的自然语言识别、工具选择质量或重试行为。

## 2. 当前 `.venv` 实际 API / 源码核验

环境：Python 3.11；`importlib.metadata.version("agentscope") == "2.0.8"`。源码路径均相对于 `.venv/lib/python3.11/site-packages/agentscope/`。

| API / 行为 | 真实字段与本地源码 |
| --- | --- |
| `RequireUserConfirmEvent` | `event/_event.py:443`；type 为 REQUIRE_USER_CONFIRM；reply_id、tool_calls；EventBase 另有 id / created_at |
| `ConfirmResult` / `UserConfirmResultEvent` | `_event.py:470/483`；confirmed、tool_call、可选 rules；结果包含 reply_id / confirm_results |
| `UserInterruptEvent` | `_event.py:496`；type 为 USER_INTERRUPT（用户中断）；reply_id；文档限定于 parked reply，主动运行时取消 task 是另一条路径 |
| `RequireExternalExecutionEvent` / `ExternalExecutionResultEvent` | `_event.py:456/521`；reply_id / tool_calls 或 execution_results；本实验 H08 使用真实外部事件类 |
| 确认产生 | `agent/_agent.py:_execute_tool_call`（2440～2579）；Tool Schema 通过后，PermissionDecision.ASK（要求确认）使 ToolCallState.ASKING（等待确认）先写入 AgentState，再 yield 原生事件 |
| 外部等待产生 | `_execute_tool_call`（2606～2619）；is_external_tool 为真、Permission 允许时，状态先设为 SUBMITTED（已提交外部执行），再发事件；本实验工具不调用真实 API |
| 流暂停 | `_reply_impl`（1230～1269）；HITL 后退出本轮流，不发 ReplyEnd；保留未完成 Tool Call，不在同一生成器里等待用户输入 |
| 确认恢复 | `_check_incoming_event` / `_handle_incoming_event`（1851～2030）；新的 reply_stream(UserConfirmResultEvent) 匹配 awaiting call，confirmed=true 设 ALLOWED（允许执行），false 形成 DENIED（被拒绝）结果，然后继续 ReAct |
| 序列化 | `state/_state.py:209`；AgentState.context 保存 Tool Call 的 asking/submitted，reply_context 保存 reply_id/cur_iter；JSON 恢复的新 Agent 可继续确认 |
| 中断 | `_reply_impl`（1066～1074 / 1288～1309）；关闭待执行调用，形成 INTERRUPTED（已中断）ToolResult 和 ReplyEnd，不继续模型推理，不发外部取消请求 |
| HTTP / Session | `app/_router/_chat.py:111` 将 HITL 结果排入 resume 队列；`app/_service/_chat.py:1402` 保存 agent.state；源码支持恢复链路，实际持久服务效果本轮未验收 |

### 2.1 框架安全边界的直接刻画

- 未知 Tool Call ID：原生 `_check_incoming_event` 抛 ValueError；完成后再提交同一结果也抛 ValueError。
- **错误 reply_id：原生框架未核对**。正确 pending Tool Call ID 搭配错误 reply_id 的拒绝事件依然形成 DENIED。适配层必须自行绑定当前 reply。
- **确认结果可修改 tool_call.name / input**，并可添加 PermissionRule。原生能力服务于可编辑权限确认，不能直接当作固定业务阶段授权。
- 原生 UserInterrupt 走早期分支，没有进入上述确认校验。本实验适配层先核对 parked reply 的身份，再传给 Agent。
- 暂停时收到普通 UserMsg 会抛 ValueError（仍在等待 event），不能直接把“先查询”当作确认或继续。可关闭交互暂停，再通过项目只读能力查询；StageRun 仍保留等待。

这些是框架边界事实，不构成在生产中放松 Resolver、Policy、Schema、ownership、版本和事务检查的理由。

## 3. A/B 架构与固定测试点

两组均使用 MockFixture、TaskCommandRunner、SessionTaskBinding、InMemoryTaskRepository 和真实 StageOrchestrator。Mock 专业工具执行 W01～W10；本轮没有真实专业算法验收。

- A `current_confirm_stage（当前阶段确认）`：读取固定目标 → POC 目标预检 → OperationInteractionController.handle(ConfirmStageRequest) → Runner.confirm_waiting_stage → StageOrchestrator / Repository → 当前 Execution 下一阶段。
- B `native_hitl_wrapper（原生 HITL 包装）`：StageRun 已持久为 WAITING_CONFIRM（等待确认） → Agent 发 Tool Call → 原生 Permission ASK → RequireUserConfirmEvent → 用户结果 → Adapter 校验原生 reply/call、禁止改名改参和允许规则 → 重查 ownership / Active / current Execution / StageRun → Runner.confirm_stage → 同一 Repository 事务 → 原生 Agent 恢复。

Tool 输入显式携带 task_id、execution_id、stage、stage_run_id；同时与 Adapter 构造时从权威进度读取的不可变目标比较。目标不只保存在事件文案中。业务确认返回后后台 Runner 调度下一阶段，原生 Agent 本身不执行下一阶段。

**A 的固定目标预检属于 POC 共用测试夹具。** 当前生产自然语言 CONFIRM_STAGE 仍是按 task_reference 解析当前等待阶段；本实验 A 的重复/旧目标拦截结果不能泛化为生产入口已具有 event_id 幂等。B 的框架重复检查也不能替代 Repository 的 StageRun 比较和确认事务。未修改当前入口的行为。

测试固定覆盖 DATA_DECODE（数据解编）和 PREPROCESS（数据预处理）两个确认点；确认预处理后检查 INTERPRET（智能解释）启动恰好一次。沿用项目当前 staged 模式，未新增按 checkpoint 配置运行的第二套状态机，未把 REPORT（报告）变成新默认确认规则。

## 4. H01～H09 结果

| 场景 | 实测结果 |
| --- | --- |
| H01 正常确认点 | 确认前只存在 W01 和一个 WAITING_CONFIRM；B 真实发出原生事件；下一阶段未启动，StageRun 快照保持一致 |
| H02 确认继续 | 两组在解编、预处理均复用现有业务确认；结果依次为 CONFIRMED（已确认）和下一阶段 WAITING_CONFIRM；步骤与 StageRun 各一次，无创建新 Execution |
| H03 拒绝 | A 不提交业务确认；B 产生原生 DENIED ToolResult 并关闭 pending；两组完成结果与 StageRun 完全不变；拒绝产品终态保持未知 |
| H04 并发重复 | 同一固定目标的两次并发提交只有一次成功；第二次失败，不会确认下一测试点；真实 Repository 仍负责事务幂等 |
| H05 旧目标 | 参数化覆盖 Active 切换、当前 Execution 替换、阶段推进、会话 ownership 改变；A/B 拒绝，旧候选与新目标不被写入；额外覆盖修改 reply/call/input/name/rules |
| H06 刷新模拟 | JSON 恢复 AgentState 后新 Agent 可用原事件继续；无 AgentState 的新 Agent 拒绝旧事件，从 StageRun 重读后新建 event 可继续；实际 HTTP/浏览器刷新及真实持久化未验收 |
| H07 等待时查询/切井 | 直接普通输入被框架拒绝；原生 interrupt 后读取 stage_result，旧等待事实仍在；真实启动另一 Mock 井后，旧事件不确认新任务 |
| H08 外部执行 | 原生外部等待被 UserInterrupt 关闭，模型未再次调用；独立 Mock 外部作业仍能完成，迟到 ExternalExecutionResult 被拒绝，业务候选不变；没有真实取消能力结论 |
| H09 报告 | 读取已生效报告直接调用现有 GetReportCommand，无 HITL；未来正式报告生成确认标记 BUSINESS_CONFIRMATION_REQUIRED（需要业务确认），不新增生产枚举或规则 |

## 5. 独立指标与复杂度

| 指标 | 结论 / 证据 |
| --- | --- |
| Native HITL Availability | 已验证当前安装版本原生类、权限产生与真实恢复调用 |
| Event Usage | 单次记录运行 28 cases；RequireUserConfirm 19，RequireExternalExecution 1；A-only 案例为 0 原生事件；附 JSONL / JUnit |
| Pause Correctness | 两个业务确认点，业务事实先持久；确认前下一阶段不启动 |
| Resume Correctness | 两个点均复用业务 confirm，阶段事实与步骤列表验证，不用 Agent 回复文本判成功 |
| Business Authority Binding | ownership / Active / Execution / StageRun 重查，固定 Tool 目标与事件改参限制；生产授权和 InputVersion 边界保留 |
| Duplicate Safety | 原生拒绝重复完成结果；同目标并发测试和现有 Repository 事务共同保证只推进一次；无分布式并发验收宣称 |
| Stale Safety | 四类旧目标与五类篡改拒绝；事件不是业务确认事实 |
| Refresh Recovery | 保存/加载 AgentState 可恢复；若缺失原交互状态，StageRun 仍是恢复锚点；不依赖第二套业务 pending |
| Interrupt Semantics | 停止 parked Agent 后续调用；独立外部作业和迟到返回单独隔离；不等于取消第三方 API |
| UI/Event Fit | 源码存在原生确认 UI、SSE 与 Session 保存；项目业务阶段面板仍走显式阶段 API；本轮无接入、无真实页面效果验收 |
| Code Complexity | 新增实验适配模块 224 行（含中文文档串），另有离线脚本模型/测试；未减少生产代码；仍需固定目标、身份绑定、业务复查、拒绝、刷新策略，不宣称简化收益 |

## 6. UI / SSE 适配与生产决策

当前仓库 `frontend/agentscope-web/frontend/src/hooks/useMessages.ts`：hasPendingToolCall 检查 asking/submitted，onUserConfirm 构造 UserConfirmResultEvent 发给 `/chat/`；历史消息＋Session SSE 源码具备原生卡片恢复逻辑。后台 ChatService 在流结束时保存 AgentState。以上为源码核验，不是浏览器实测。

项目现有 `src/cnlc_agent/demo/agentscope_app.py` 的 `/cnlc/interpretation/.../stages/{stage}/confirm` 接受 expected_stage_run_id，复用 Runner.confirm_stage；自然语言 CONFIRM_STAGE 走 Operation。**没有把原生 Tool 确认卡片绑定到业务阶段面板。** 若未来接入，应在受控 Tool / Adapter 和确认提交入口核对身份，禁止直接接收前端改写后的阶段目标或永久允许规则；接入前须补实际页面、HTTP、持久化与竞态回归。

本轮生产阶段交互仍用现有 CONFIRM_STAGE / StageRun，结论 NO_CLEAR_VALUE；原生局部交互包装作为未来可选 WRAP 候选。Tracing、最终方案定稿、真实业务 B01～B08 均未开展。

## 7. 验证与复现

```bash
.venv/bin/pytest tests/experiments -q
.venv/bin/pytest tests/unit -q
.venv/bin/pytest tests/integration/test_task_react.py -q
.venv/bin/pytest tests/integration/test_interaction_robustness.py -q
.venv/bin/pytest -q
.venv/bin/ruff check experiments/agentscope_native_poc/hitl_runner.py \
  tests/experiments/test_hitl_interrupt_poc.py \
  tests/experiments/test_native_poc_contracts.py \
  tests/integration/test_interpretation_read_api.py
git diff --check
```

已执行指定回归：experiments 80、unit 745、task_react 30、interaction_robustness 43 项通过。完整套件结果见 Task 完成记录；真实数据库/Redis、真实模型和真实 GDSX 的 opt-in 测试缺环境时跳过，不能当作这些能力通过。

若要重录事件，应显式指定新的输出文件，避免追加到已有记录：

```bash
CNLC_HITL_EVIDENCE=/tmp/task012d-hitl-events-new.jsonl \
  .venv/bin/pytest tests/experiments/test_hitl_interrupt_poc.py -q \
  --junitxml=/tmp/task012d-hitl-tests-new.xml
```

既有测试修正：012A 的 import 隔离断言改为子进程，不受 integration 或 012D 合法导入 Runner 污染；其 AST 隔离只允许 012D 指定模块例外，其余旧 POC 约束不变。完整套件还暴露 Read API 重启测试替身仍挂在 `_redis_storage`，而 postgres-redis 入口已走 `_conversation_storage`；单测 RED 复现后把替身挂到实际入口，不改生产行为。该测试仍只验证业务 Binding，真实 Conversation 持久化由专用 opt-in 测试负责。

## 8. 架构与文档影响

No Architecture Issue found.

本轮只新增实验和事实记录，没有核心 Agent、Workflow、版本、权限、数据库或 Redis 架构迁移。原生 reply_id 未校验、可改 Tool 参数等差异记录在本证据中，并由实验 Adapter 限制，未影响现有生产控制链。

Documentation Impact：更新主设计 04 / 05、Task 012 / 012D、docs 与 evidence 导航、POC README；新增本证据及事件/JUnit/摘要产物。检查 01～03：产品需求、目标和可观察效果未改变，无需修改。检查 08 / 09 / 10 和状态词典：生产交互、SSE/UI、状态机和稳定枚举未改变，无需修改。没有代码已变而对应文档未同步的遗留。下一阶段依赖人工评审本证据，以及 Q05、报告生成确认语义和第三方取消能力的独立业务/接口验证。
