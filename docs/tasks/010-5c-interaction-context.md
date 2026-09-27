# Task 10.5-C：交互上下文分离与安全生命周期

## 边界与 Git 基线

日期：2026-09-27。本次仅建立交互焦点模型、runtime 状态管理、旧入口兼容和 Conversation 归档过滤。
业务事实仍来自 PostgreSQL 的 Task、InputVersion、Execution、ToolRun、SessionTaskBinding 与 Report。

- 分支：`codex/task-10-5-operation-understanding`。
- 开始 HEAD：`d802ea398d8d978fea984944548e23f286ba890a`。
- Task 10.5-A：`5a552af6252f9a4555ca8e1fdf37f02cf7757341`。
- Task 10.5-B：`49b1016f4f337088bcec2142ffd7dd15e8f0ff3b`。
- 稳定基线 `origin/codex/demo-2026-10-31`：`ba727747c9a8a114364e40ce0650e679b1134bd5`。
- 开始前执行 fetch、switch、pull --ff-only；已包含 A/B 和多道测井曲线稳定基线。
- 开始时已跟踪文件干净；既有 `.idea/`、两份 `demo_output` JSON、
  `docs/gdsx-tool-function-inventory.md` 保留，不纳入提交。
- 提交：`feat: separate active view and recent contexts`，SHA 见完成报告。
- 未修改 main / 稳定分支，未创建 PR。

已阅读项目架构、交互设计、状态机、枚举表、Conversation 持久化及 10.4、10.5-A/B 记录；
检查 Operation 模型、Capability Catalog、Task/Execution/Scope Resolver、TaskRepository、Binding、
TaskCommandRunner、交互中间件与 AgentState.middle_context。

## 文件

新增：

- `src/cnlc_agent/demo/interaction_context.py`
- `tests/unit/test_interaction_context.py`
- `docs/tasks/010-5c-interaction-context.md`

修改：

- `src/cnlc_agent/demo/task_tools.py`
- `src/cnlc_agent/infrastructure/conversation.py`
- `tests/unit/test_conversation_persistence.py`
- `tests/integration/test_conversation_storage_integration.py`
- `docs/12-conversation-persistence.md`

## 模型与可信边界

所有模型复用 Contract、NonBlank 与 OperationScope；禁止多余字段，ID 不接受空白字符串。
未新增正式枚举，因此无需修改枚举表。

| 模型 | 字段与约束 |
| --- | --- |
| ActiveContext（当前操作上下文） | 必填 task_id；可选 base_execution_id、scope；scope 非空必须有 base_execution_id。 |
| ViewContext（当前查看上下文） | 必填 task_id；可选 execution_id、scope；scope 非空必须有 execution_id。 |
| RecentContext（近期交互上下文） | 可选 last_operation_id、last_compare、last_scenario_id；selected_intervals 默认独立空列表。 |
| InteractionContext（整体交互上下文） | 可选 active、view；recent 默认独立空对象。 |
| CompareContext（近期比较引用） | 必填 left、right；两边均为 ContextExecutionReference（具体任务版本引用），必填 task_id 和 execution_id，可选 scope。 |
| SelectedIntervalReference（选中层段引用） | 必填 task_id、execution_id、interval_id；不能仅记录层号。 |

这些对象不查询 Repository、不创建 Execution、不保存比较结果或场景结果，不证明 ID 存在或归属合法。
`ACTIVE_BASE`（当前工作基线版本）是用户交互意图，可以选择 V2，而业务 Task 的
`TASK_CURRENT`（任务当前最新版本）仍为 V4。保存基线不修改 Task.current_execution_id。
真正执行时必须由 Task 10.5-B OperationReferenceResolver / ScopeResolver 重新核验 Binding、任务、
版本和层段；没有 trusted 标记或绕过授权的入口。

## runtime 管理与兼容入口

InteractionContextStore（交互上下文状态管理器）使用以下 middle_context key：

- `cnlc_interaction_context`：完整 JSON 可序列化上下文，是新格式 runtime 主表示。
- `cnlc_active_task_id`：兼容旧入口的任务焦点 hint，与新格式 active 同步。
- `cnlc_pending_clarification`：保持原有独立生命周期，不由本状态管理器解释。

TaskCommandRunner.active_task_id 保留为只读兼容属性，旧代码仍调用 set_active_task 更新。
它直接读取状态管理器中的 active，避免两份内存焦点漂移。interaction_context 返回深复制快照；
更新 API 重新验证并隔离嵌套对象，非法更新不写入 runtime。

| API | 行为 |
| --- | --- |
| set_active_task(task_id) | 同步新旧 key；相同任务保留 base/scope，切任务清除旧 base/scope；独立的 View/Recent 保留。 |
| set_active_base(task_id, execution_id, scope=None) | 必须已存在同一 active task，否则 ValueError 且状态不变；只保存基线 hint。切换基线未传 scope 时清除旧 scope。 |
| set_view_context(task_id, execution_id=None, scope=None) | 只更新 View，不改变 Active 或 legacy active task。 |
| clear_view_context() | 仅清除 View。 |
| set_recent_context(recent) | 整体替换 Recent，不改变 Active/View。 |

attach_session_runtime_context 每次按传入 Session 状态恢复，不沿用前一次 Session 的短期状态：

1. 合法新格式优先，即使与 legacy 不一致也以新格式为准，并同步 legacy。
2. 新格式没有 active 时清除过期 legacy key。
3. 只有合法 legacy 时重建最小 ActiveContext；不推断 base、scope、View、Recent。
4. 新格式损坏时安全丢弃整份，尝试合法 legacy；legacy 也不合法则为空，不阻断 Agent 初始化。

## 刷新与持久化生命周期

浏览器刷新且 Redis Session 命中时，完整新上下文随 AgentState 序列化、反序列化保留。
Conversation 列表读取不会覆盖已命中的缓存。PendingClarification 仍执行原来的 owner、TTL、紧邻轮次
检查，不因保存完整交互上下文而延长其生命周期。

`_durable_session` 在归档前深复制 SessionRecord，只从归档副本删除：

- `cnlc_pending_clarification`
- `cnlc_interaction_context`

保留 `cnlc_active_task_id` 和其他原有 durable 会话、消息内容；不会原地修改 AgentState。
Redis 丢失后，从 PostgreSQL 恢复最多得到 legacy task hint，attach 后只有 active.task_id；
base_execution_id、active.scope、view 均为空，recent 为默认空值。
不通过 Conversation Message 自动重建旧版本、旧层段、比较或场景引用。需要时重新澄清，避免
用户后续省略对象的修改请求意外复用长期归档中的旧写范围。

## 现有交互兼容与未实现

现有 `STATUS`（查询状态）、`GET_REPORT`（获取报告）、`MODIFY`（修改解释）、
`FULL_RERUN`（完整重跑）成功后仍按原路径调用 set_active_task。
interaction_snapshot 继续使用 active_task_id，未切到 View。为避免读操作路由半接入，统一的
Read → View / Write → Active 规则留待 10.5-E 集中接入。

未实现 Task 10.5-D/E 的自然语言解析、PlanValidator、PendingClarification V2、Capability 裁决、
ReAct OperationPlan 集成、Compare/Scenario Engine 或 Task 11 DependencyGraph。
没有修改 ReAct prompt、Tool Contract、MockTaskShellModel、专业 Tool/算法、Heavy API 或 W01–W10。
没有修改 Task 10.5-A/B 语义、数据库 Schema；没有新增表、migration、依赖或前端改动。

## 测试结果

| 检查 | 结果 |
| --- | --- |
| `uv run pytest -q tests/unit/` | 400 passed；含新增 36 个上下文用例和 Conversation 单元回归。 |
| `uv run pytest -q tests/integration/test_interaction_robustness.py tests/integration/test_task_react.py` | 61 passed。 |
| `tests/integration/test_conversation_storage_integration.py` | 真实 PostgreSQL + Redis：2 passed，无跳过。 |
| Ruff：本次全部 6 个新增/修改 Python 文件 | 通过。 |
| mypy：本次全部 3 个新增/修改生产 Python 文件 | 通过。 |
| `git diff --check` | 通过。 |

真实 Conversation 集成使用本地 ConnectionSettings，仅将数据库/Redis 连接映射到测试环境变量；
没有输出连接串。测试覆盖真实归档过滤、缓存命中完整恢复、状态更新过滤、删除当前测试会话缓存后
PostgreSQL 恢复、业务 Binding 保留。通过删除测试会话缓存模拟 miss，不执行全局 Redis flush。

新增单元测试覆盖模型约束、任务切换清理、基线不匹配拒绝、View/Recent 隔离、无效更新原子性、
深复制隔离、新旧格式冲突与损坏回退、仅 legacy 恢复，以及 AgentState JSON 完整 round-trip。

Task 10.5-C 尚未接入新的用户可见 Operation 交互，因此本次无新增页面行为；页面级完整验收将在 10.5-E 后执行。
未进行人工页面测试；前端 Contract 未变，未重新执行前端 build。

## Architecture Issue

No Architecture Issue found.

下一阶段依赖为已完成的 A/B/C；本次完成后停止，不启动 Task 10.5-D。
