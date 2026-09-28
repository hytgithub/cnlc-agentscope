# Task 10.5-E2：ReAct Operation 集成与页面验收

## 范围与基线

- 开发分支：`codex/task-10-5-operation-understanding`。
- 开始 HEAD：`14bcda904fd9c0dd053916e988e209a3eb0634c0`，包含 A 至 E1.1。
- 本次接入现有 OperationPlan、Resolver、PlanValidator、ClarificationStore、ExecutionBridge 与 TaskCommands；W01-W10、数据库 Schema 和 migration 均未修改。
- 原有 `.idea/`、两份 `demo_output` JSON、`docs/gdsx-tool-function-inventory.md` 属于工作区已有文件，本次不提交。

## 文件清单

新增：

- `src/cnlc_agent/demo/operation_interaction.py`
- `src/cnlc_agent/demo/operation_tool.py`
- `tests/unit/test_operation_tool.py`
- `tests/integration/test_real_operation_react.py`
- `docs/tasks/010-5e2-react-operation-integration.md`

修改：

- 核心设计文档：`docs/03-system-architecture.md`、`docs/08-intent-and-interaction-design.md`、
  `docs/10-interaction-state-machine.md`、`docs/11-status-enum-glossary.md`、
  `docs/12-conversation-persistence.md`。
- 后端：`src/cnlc_agent/demo/agentscope_app.py`、`demo_agent.py`、
  `interaction_middleware.py`、`operation_clarification.py`、`operation_execution_bridge.py`、
  `operation_parser.py`、`plan_validator.py`、`scope_resolver.py`、`task_tools.py`（均位于同一目录）。
- 前端：`frontend/agentscope-web/frontend/src/components/chat/messageVisibility.ts`、
  `tool-renderers/RunWellInterpretationRenderer.tsx`、`tool-renderers/index.tsx`，以及
  `frontend/agentscope-web/frontend/src/hooks/useInterpretationTask.ts`。
- 测试：`tests/integration/test_demo_agentscope_web.py`、`test_demo_web_http.py`、
  `test_demo_web_upload.py`、`test_interaction_robustness.py`、`test_session_task_binding.py`、
  `test_task_react.py`（均位于 `tests/integration/`），以及 `tests/unit/test_task_tools.py`。

## 正式调用链

```text
自然语言 → AgentScope ReAct / qwen-plus
→ interpret_interpretation_operation
→ OperationInteractionController
→ Context / Task / Execution / Scope Resolver
→ PlanValidator → OperationExecutionBridge
→ TaskCommands → 现有 Workflow W01-W10
```

正式模型 Toolkit 只有 `run_well_interpretation`（明确 Fixture 井号首次解释）和
`interpret_interpretation_operation`（已有任务后续操作）。上传仍由
UploadInterpretationReply 处理。旧五个 direct Tool 和 `build_task_tools()` 保留供低层调用、
兼容与回归；正式 ReAct 白名单阻止模型使用它们、专业 Tool、文件系统、MCP 与内置工具，
因此后续写操作必须经过完整计划裁决。

统一 Tool Contract：输入是顶层 `request` 的严格 Pydantic 判别联合；输出是
`outcome`、`error_code`、`message`、`task_results`、`created_execution_ids` 与
`capability_facts`。异常投影为安全错误码和文案，执行状态以既有 TaskCommandResult 和
持久化 Execution 为准。Tool 自身无外部专业调用，超时沿用现有仓库、缓存与后台执行策略。
离线 Mock 和真实模型共用这个输入 Schema 与业务 Bridge。

| Mode | 输入 | 服务端行为 |
| --- | --- | --- |
| PLAN（新计划） | `PartialOperationPlan` | 全计划解析与校验；缺槽可保存 Pending。 |
| CLARIFICATION_REPLY（澄清回复） | `ClarificationPatch` | 只修补 Pending 允许的槽位；执行前消费，防重放。 |
| CANCEL（取消） | 无业务参数 | 清除 Pending，0 Task、0 Execution，固定安全回复。 |
| SET_ACTIVE_CONTEXT（设置操作焦点） | `task_reference`，可选版本与范围 | 采用 WRITE（写安全）引用解析，更新 Active，清除旧 View，0 Execution。 |

原始 ToolCall 在 AgentScope 自动参数修复前再做严格 Schema 校验；未知字段、模型伪造的
`interval_id`、把引用塞入 `parameters` 等都拒绝，不能因框架清理字段而丢失范围。
框架更早抛出的参数错误转为固定安全回复并终止本轮。新的绝对数值 PLAN 必须能在本轮用户原文
找到该数值（百分数按数值形式折算），防止模型在取消后从聊天记忆复活旧修改值；此校验只验证
数值来源，不承担专业计算或自然语言意图分类。中文数字等尚不能通过此来源校验，要求用户改用
阿拉伯数字重述。

## Pending 与引用安全

TaskCommandRunner 持有唯一 OperationClarificationStore 及 owner token。每轮 AgentScope
重新 attach `middle_context` 时只替换上下文引用，不重建 owner。Pending 保存在
`cnlc_pending_operation_clarification`，与现有交互回合计数、短期 TTL 同步；只信任 Resolver
成功授权的锁定 Task / Execution / Scope。新 PLAN、CANCEL、无关下一轮、过期、owner 改变或
Redis miss 清除 Pending；PostgreSQL 不持久化它，也不从聊天记录恢复。普通浏览器刷新保留
同进程 Runner 与 Redis 状态。CORRECTION（修正）只针对未执行 Pending，可撤销相关锁并重新
解析；已经提交的 Execution 不会原地改写。

模型只表达 `INTERVAL_ORDINAL`（单层号）或 `MULTI_INTERVAL_ORDINAL`（多层号），例如
`ordinal=5`。ScopeResolver 在已授权的目标 Execution 中把层号映射为该版本真实的层段 ID；
正式可执行 OperationPlan 只接受稳定 Scope。当前局部写与局部重算能力未开放，层号不存在
明确失败，存在也安全拒绝，绝不退化为 WHOLE_WELL（整井）写操作。模型输入不得含稳定层段 ID。

查看历史报告只更新 View，不改变 Active 或 `Task.current_execution_id`。V3 的
PREVIOUS（上一版）以当前 View V3 为锚点得到 V2。异井 View 与 Active 之间的隐式写触发
澄清，不创建 Execution；只有明确 SET_ACTIVE_CONTEXT 才改变写焦点。显式设置焦点时清除
旧 View，包括同井历史锚点，避免后续隐式写继续受旧查看版本影响。

## 提示词、测试与边界

系统提示词给出双 Tool 边界、一个 Tool Call 内合并多个意图、缺槽、取消、报告、层号、
能力询问和条件请求的代表性示例。数值参数固定放在 `parameters.value`，版本引用固定放在
Operation 节点；模型不得决定 W01-W10、`start_step`、RUN/REUSE 或专业参数。
MockTaskShellModel 仅供确定性测试，按相同 Operation Schema 产生 ToolCall。真实
qwen-plus smoke 使用既有 DashScope 连接和专业 Mock fixture；不记录凭据。

真实模型曾把数值置于节点顶层、把版本引用放入 `parameters`，以及在条件计划顶层附加
`tool_call_id`。前两者经提示词与 Schema 描述修复；最后一种请求被原始 Schema 校验安全拒绝，
没有业务写入。模型仍可能产生无效结构，这是调用质量限制；服务端保持零副作用拒绝。
页面还发现显式切回同井时旧历史 View 会挡住隐式写入，现已清除所有旧 View 并增加回归；
真实模型的状态查询偶尔省略 `persist_mode`，现仅对纯只读 PLAN 补齐该无副作用字段，
写计划仍须完整校验。取消后模型若误发 CLARIFICATION_REPLY，服务端拒绝过期补丁，
旧数值不会恢复。

当前未开放 COMPARE（对比）、SCENARIO（试算）、SEGMENT_EDIT（层段编辑）、
MODIFY_RESULT（修改派生结果）、任意 RECALCULATE（重新计算）、REINTERPRET（重解释）、
历史版本分支写、局部重跑、TVD/TVDSS 转换、Evidence Pipeline 和人工 Override。
条件计划整体拒绝；能力询问只读并返回能力目录事实。

## 验证记录

| 检查 | 结果 |
| --- | --- |
| Unit + 核心 integration | 720 passed；Starlette 第三方弃用告警 1 条。 |
| 真实 PostgreSQL / Redis 四组 integration | 14 passed。 |
| 真实 qwen-plus ReAct smoke | 1 passed，覆盖多参数单次执行、跨轮澄清、能力询问、局部范围、条件拒绝与取消。 |
| Frontend `pnpm test` | 8 passed。 |
| Frontend `pnpm build` | 通过；仅包体积提示。 |
| Ruff、mypy、`git diff --check` | 通过。 |

HTTP `/chat/` 与 `/sessions/.../stream` 回归覆盖统一 Tool、上传、修改、上一版报告、
状态、跨请求 Pending 补齐和不支持操作。流式执行与原有过程折叠、计时、报告、右侧版本记录
共用现有展示链。

## 真实页面验收

环境：本地 Vite 前端、AgentScope/Uvicorn 后端、真实 qwen-plus，专业计算采用项目 Demo Mock，
真实 PostgreSQL / Redis。会话中由用户通过页面上传 `WELL_MOCK_PLOT_001` 演示井资料；
另以明确 Fixture 井号启动 `WELL_MOCK_001`。A 表示 `WELL_MOCK_001`，B 表示
`WELL_MOCK_PLOT_001`。页面操作与 PostgreSQL Execution 序号、Redis Active/View
交叉核对。状态名含义见状态词汇表。

| 场景 / 页面输入 | 模型 Tool | 页面和持久事实 | 新 Execution | Active / View | 结果 |
| --- | --- | --- | --- | --- | --- |
| 上传 B 井资料并首次解释 | 上传中间件 | B/V1 WARNING（完成但有告警）；报告和多道曲线可见。 | B +1 | Active=B | 通过 |
| `解释 WELL_MOCK_001` | run_well_interpretation | A/V1 SUCCESS（成功），W01-W10 与报告可见。 | A +1 | Active=A | 通过 |
| `孔隙度改成0.16` | 统一 Operation PLAN | A/V2 SUCCESS，W01-W03 复用，W04-W10 调用轨迹与报告可见。 | A +1 | Active=A | 通过 |
| `孔隙度、渗透率都改成0.16` | 统一 Operation PLAN，一次调用、两节点 | A/V3 SUCCESS，只有一个版本。 | A +1 | Active=A | 通过 |
| `改成0.17` → 刷新 → `孔隙度` | PLAN → CLARIFICATION_REPLY | 首轮缺目标，第二轮 A/V4 SUCCESS；Pending 刷新恢复。 | 0 → A +1 | Active=A | 通过 |
| `全部重跑` | 统一 Operation PLAN | A/V5 SUCCESS，W01-W10 全部 RUN，参数保留，右侧版本列表更新。 | A +1 | Active=A | 通过 |
| `给我上一版报告` | 统一 Operation PLAN/REPORT | 页面出现完整历史报告；A 当前仍 V5。 | 0 | View=A/V4 | 通过 |
| `你能只重新算Sw吗` | 统一 Operation PLAN/CAPABILITY_QUERY | 明确未开放，未偷偷全量重跑；A 仍 V5。 | 0 | 不变 | 通过 |
| `第5层孔隙度改成0.16` | 统一 Operation PLAN，层号 5 | 目标版本没有第 5 层，明确返回层号不存在；A 仍 V5。 | 0 | 不变 | 通过 |
| `切到 WELL_MOCK_PLOT_001 继续处理` | 统一 Operation SET_ACTIVE_CONTEXT | 页面确认切换操作焦点；无版本增加。 | 0 | Active=B，异井 View 清除 | 通过 |
| `查看 WELL_MOCK_001 第3版报告` → `给我上一版报告` | 统一 Operation PLAN/REPORT | 页面显示 V3 再显示 V2；A 当前仍 V5。 | 0 | Active=B、View=A/V2 | 通过 |
| `孔隙度改成0.18`（跨井 View 后） | 统一 Operation PLAN | 明确要求指定操作井；A 仍 V5、B 仍 V1。 | 0 | Active=B、View=A/V2 | 通过 |
| `切到 WELL_MOCK_001 继续处理` → `孔隙度改成0.18` | SET_ACTIVE_CONTEXT → PLAN | 清除旧历史 View；A/V6 SUCCESS（成功），B 保持 V1。 | A +1 | Active=A、View 清除 | 修复后通过 |
| `改成0.19` → `算了` → `孔隙度` | PLAN → CANCEL → 无效澄清补丁 | 首轮待澄清，取消返回固定文案，后续参数名未恢复旧值；A 保持 V6。 | 0 | Active=A、Pending 清除 | 通过 |
| 刷新页面 → `现在到哪一步了` | 统一 Operation PLAN/STATUS | 右侧 A/V6 历史与报告恢复；回复 V6 SUCCESS（成功）、W01-W10 已完成。 | 0 | Active=A | 修复后通过 |

页面上点击“解释过程”折叠后正文确实隐藏，展开后正文恢复，报告始终可见；计时位于
过程按钮，参数修改后报告内容更新。刷新后右侧执行历史和报告仍可见。
曲线图全屏操作显示 6 条采样曲线与层段结论，关闭后回到聊天页面；截图保存在本次验收工作区。

## Architecture Issue

No Architecture Issue found.
