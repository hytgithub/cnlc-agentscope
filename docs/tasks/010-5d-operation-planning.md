# Task 10.5-D：复合计划、上下文补全与通用澄清

## 1. 范围与 Git 基线

日期：2026-09-27。仅实现纯规划层，完整计划在任何业务写入前统一校验。本次未将新接口接入
用户消息执行链，不创建业务数据、不调用模型或 Tool，不修改专业 Workflow。

- 开发分支：`codex/task-10-5-operation-understanding`。
- 开始 HEAD / 10.5-C：`55dadaeb0c894e343e3acff8886439f197528595`。
- 10.5-A：`5a552af6252f9a4555ca8e1fdf37f02cf7757341`。
- 10.5-B：`49b1016f4f337088bcec2142ffd7dd15e8f0ff3b`。
- 稳定基线：`origin/codex/demo-2026-10-31`，HEAD `ba727747c9a8a114364e40ce0650e679b1134bd5`。
- 开始前执行 fetch、switch、pull --ff-only；开发分支相对稳定基线 ahead 4 / behind 0。
- 已跟踪工作区干净；保留 `.idea/`、两份 `demo_output` JSON、
  `docs/gdsx-tool-function-inventory.md`，未纳入提交。
- 一个 Task 一个 commit：`feat: validate compound operation plans and clarification`；SHA 见完成报告。
- 不修改 main 或稳定分支，不创建 PR。

已阅读项目架构、交互设计、状态机、枚举、Conversation 文档和 A/B/C 验收记录；
检查 Operation 模型/Catalog、引用和范围解析器、上下文管理、旧澄清/中间件/Task Tools 与持久化测试。

## 2. 文件

新增生产文件：

- `src/cnlc_agent/demo/operation_parser.py`
- `src/cnlc_agent/demo/operation_context_resolver.py`
- `src/cnlc_agent/demo/plan_validator.py`
- `src/cnlc_agent/demo/operation_clarification.py`

新增对应四个单元测试文件：`tests/unit/test_operation_parser.py`、
`test_operation_context_resolver.py`、`test_plan_validator.py`、`test_operation_clarification.py`。
新增本文档。

修改：`src/cnlc_agent/infrastructure/conversation.py`、
`tests/unit/test_conversation_persistence.py`、`tests/integration/test_conversation_storage_integration.py`、
`docs/11-status-enum-glossary.md`、`docs/12-conversation-persistence.md`。

## 3. Partial 与 Complete

PartialOperationNode（待补全操作节点）包含 operation_id、已知 action、可选 target、
任务/版本/范围引用、parameters、constraints、input_refs、output_alias。
PartialOperationPlan（待补全操作计划）包含 input_classification、shared_context、operations、
edges、conditions、可选 persist_mode、output_requirement、original_instruction。

StructuredOperationParser（结构化操作解析器）仅支持 dict/JSON。未知字段、未知动作和非法枚举
由 Schema 拒绝；不扫描自然语言、不调用 qwen-plus。不明确 action 时不构造万能待处理节点。
parameters.extensions 是不执行的元数据；不能在任意层级藏正式字段、trusted 或 locked 引用。

`finalize_partial_plan` 返回 FinalizationResult（补齐结果）。缺 target、persist_mode、修改值、
切换方法/模型标识或比较输入时返回结构化 ClarificationIssue（澄清问题），不猜测。
完整 OperationNode.target 与 OperationPlan.persist_mode 保持原来的必填约束；A 的模型没有修改。
引用字段仍遵循 A 的可选 Schema；finalize 成功只代表可构造完整计划，不等于图、能力或授权通过。

## 4. Context Resolution

OperationContextResolver（操作上下文补全器）返回 ContextResolutionResult（候选计划及问题）。
输入可为 Partial 或 Complete，输出是独立副本，不访问 Repository 或修改 InteractionContext。
Validator 可以直接接收该结果，保留其中的阻断 issues。

| 情况 | 规则 |
| --- | --- |
| 节点显式引用 / shared_context | 节点优先于共享；二者都优先于交互上下文，不覆盖显式任务、版本、范围。 |
| 只读动作 | QUERY（查询）、EXPLAIN（解释）、COMPARE（比较）、HISTORY（历史）、REPORT（报告）、STATUS（状态）优先使用 View，无 View 时使用 Active。 |
| 写动作 | 只从 Active 取任务、工作基线、范围；其他未列入只读集合的动作也采用此边界。 |
| Active=A，View=B，写任务未指定 | VIEW_ACTIVE_CONTEXT_CONFLICT（查看与操作上下文冲突）；不选择任一任务。 |
| Active=A、无基线，View=A/V1，写版本未指定 | 同样要求澄清，不自动把旧 View 作为基线。 |
| Active=A/V2，View=A/V1 | 明确工作基线 V2 优先，生成具体 Execution ID 候选。 |
| 显式 Task=B，Active=A | 保留 B，不拼接 A 的版本或范围；WELL_ID 等符号引用不能在内存中冒充已经解析。 |
| 显式版本不同于上下文版本 | 保留显式版本，不继承另一个版本的局部范围。 |
| 无可用写任务 | 返回 TASK（任务对象）槽位，View 不会转成写任务。 |

没有上下文基线时保留未指定版本，不偷偷选择历史 View，也不把 TASK_CURRENT（任务当前最新版本）
伪装成已验证的具体版本；E 仍需结合 B 解析。Recent 不会自动注入任何操作。
`resolve_recent_selection`、`resolve_last_compare` 仅提供明确调用的副本读取 API。
所有候选 ID 均为 hint，执行前仍须 B 的 Binding/Task/Execution/Scope 校验。

## 5. PlanValidator 与整体裁决

PlanValidator（计划校验器）只依赖注入的 OperationCatalog，不持有业务服务、Repository、dispatcher
或模型。PlanValidationResult（计划校验结果）包含 outcome、可选完整 plan、issues、capability_facts、
capability_issues、missing_slots、conflicts、read_only、graph_valid。

复用 ResolutionOutcome（解析结果）：EXECUTABLE（可进入写处理）、READ_ONLY（只读）、
NEED_CLARIFICATION（需要澄清）、KNOWN_UNSUPPORTED（已识别但当前不支持）、REJECTED（已拒绝）。
失败时 plan 为空，不返回可执行子计划；能力询问也不返回写计划。
EXECUTABLE 只是纯规划资格，不能绕过 B 与既有 Application 的执行前校验。

### 图与多意图

校验 operation_id 唯一、所有 edge/input_refs/conditions/output_requirement 的节点引用存在、
无自环、无依赖环。显式边与 input_refs 一并参与拓扑检查，避免只检查显式边遗漏循环。
非法结构返回 INVALID_OPERATION_PLAN（操作计划结构非法）与 REJECTED。

合法的“修改 → 重算 → 比较”和“模型 A / 模型 B 并行 → 比较”都可表达。
图结构合法与业务能力开放分别报告；不把多个 Operation 视为天然冲突。
Operation 不等于 Execution，不决定合并成几个业务执行，不生成 W01–W10 DependencyGraph。

### 能力与必需信息

- Catalog 的 ENABLED（已启用）才有继续规划资格；DISABLED（已禁用）、UNVERIFIED（待业务确认）、
  NOT_IMPLEMENTED（尚未实现）均阻断，并保留原状态。缺 Catalog 声明也安全阻断。
- 显式范围和参数名检查 Catalog 的 allowed_scopes / supported_parameters；不调用 handler_name。
- Catalog 尚未建模的专业参数规则、单位换算和执行适配继续由现有 Command / 未来 E 负责，
  Validator 不新增专业阈值，也不把规划通过视为业务契约通过。
- MODIFY_RESULT（修改结果）、MODIFY_PARAMETER（修改参数）要求 ValueSpec（修改值规格）。
- SWITCH_METHOD（切换方法）要求 method_id；SWITCH_MODEL（切换模型）要求 model_id。
- 比较至少两个不同的结构化 input_refs，可为节点输出或版本引用；不自动补上一版。
- CAPABILITY_QUERY（能力询问）对合法图始终只读，即使询问的是已启用写动作；无需执行槽位齐全，
  只返回 Catalog 事实。OUT_OF_DOMAIN（领域外请求）拒绝，即使误带已启用动作。
- READ_REQUEST（只读请求）夹带写动作视为冲突。澄清、修正、确认、取消、元请求不作为独立写计划执行；
  回复必须先按相应交互语义处理，不能通过分类绕过边界。

### 条件、跨任务和冲突

- conditions 非空：CONDITIONAL_EXECUTION_UNSUPPORTED（条件式自动执行当前不支持），
  KNOWN_UNSUPPORTED；只校验引用，不求值、不循环。
- 多个写节点指向不同明确 task_id：CROSS_TASK_WRITE_UNSUPPORTED（跨任务复合写当前不支持），
  KNOWN_UNSUPPORTED。纯只读跨任务比较不因此被拒绝。
- 多写节点任务归属尚不明确时要求 TASK 槽位；可由 B 的调用方传入 operation_id → task_id
  的 resolved_task_ids 消除歧义，此映射不是用户 Schema 字段，不代表本 Validator 执行了授权。
- CREATE_VERSION（创建版本）与 DO_NOT_PERSIST（不保存）冲突；PREVIEW（预览）与
  COMMIT_SCENARIO（提交试算）冲突；NO_FULL_RERUN（禁止全量重跑）与 FULL_INTERPRET（完整解释）冲突。
- ONLY_SCOPE（仅限指定范围）与 EXCLUDE_SCOPE（排除指定范围）仅在显式集合或同深度基准几何能证明
  全部排除时判冲突。无法确定层段和深度之间的关系时要求澄清，不查询或猜测业务数据。
- 冲突返回 MULTI_OPERATION_CONFLICT（复合操作存在冲突）。缺比较对象返回
  COMPARE_TARGET_REQUIRED（比较对象不足）。缺槽或可澄清冲突阻断整个计划。

裁决先检查结构和分类；条件式/跨任务写为硬阻断，其余缺槽和冲突优先于普通能力缺口。
因此“合法修改 + 缺比较对象”返回整个计划需要澄清，即使修改自身可执行，也不会先提交修改。

## 6. 通用澄清与 Correction

ClarificationSlot（澄清槽位）包含 TASK、EXECUTION（版本对象）、SCOPE（作用范围）、TARGET（操作目标）、
VALUE（修改值）、METHOD（方法或模型）、COMPARE_TARGET（比较对象）、PERSIST_MODE（保存方式）、
CONFLICT_RESOLUTION（冲突选择）。完整中文表见枚举表第 35 节；不另加重复 MODEL 枚举。

PendingOperationClarification（通用待澄清计划）保存 partial_plan、issues、owner_token、created_turn、
expires_at、original_instruction 和 locked_references。
LockedOperationReference（固定操作引用）包含 operation_id、task_id、可选 execution_id/scope。
`from_resolved` 接受 B 的解析结果并检查其任务/版本一致；未来 E 只能在 B 成功后从服务端写入锁。
Python 内部模型构造本身不是授权；外部 Parser/Patch 不接受 trusted/locked 字段。

保存时将已固定任务/版本/范围写入对应节点。随后 Active 改变也不会让普通补槽漂移到另一井。
固定 task 的同时可以补尚未锁定的 execution；已经锁定的具体字段不能被普通回复替换。

ClarificationPatch（结构化澄清修补）仅开放节点、任务、版本、范围、目标、值、方法/模型、比较输入和
保存方式。普通 CLARIFICATION_REPLY（澄清回复）只能填对应节点的待缺槽位；只缺 TARGET 时，
附带修改 task/execution/value/persist_mode 会以 CLARIFICATION_SLOT_INVALID（澄清槽位修补非法）拒绝，
Pending 保持不变。多节点回复必须明确 operation_id。显式 null 不视为补槽。

CORRECTION（修正上一输入）可替换尚未执行的 Partial Plan 字段。修正 Task 时清除旧版本和范围；
修正版本时清除旧范围；修正引用会撤销该节点的旧锁，后续必须重新调用 B。
共享引用先展开到节点，防止纠正后又从 shared_context 继承旧版本。修补后重新检查缺槽，
还必须重新进行完整 Context/Plan 校验。已 consume 或失效的计划不能再修正，更不能修改历史 Execution。

## 7. 生命周期与 Conversation

OperationClarificationStore（通用澄清状态存储）只写 `cnlc_pending_operation_clarification`。
提供 save、peek、apply_patch、consume、clear、cancel。TTL 由调用方传入，可复用现有配置；
owner 由服务端随机生成。只允许创建轮次及紧邻下一轮，修补不续期、不改创建轮次。
owner 不一致、TTL 到期、轮次不匹配、损坏/不一致 payload 均清除。新独立计划 save 替换旧 Pending，
无关独立操作/显式取消由未来 E 调用 clear/cancel；不在这里识别“算了”等自然语言。

新 key 与 `cnlc_pending_clarification` 独立，旧 save_pending/pending_clarification 完全不变。
Redis hit 保存新 Pending；新 backend Store 无法沿用旧 owner。durable sanitizer 在深复制后删除：

- `cnlc_pending_clarification`
- `cnlc_pending_operation_clarification`
- `cnlc_interaction_context`

仍保留 `cnlc_active_task_id` 兼容 hint。Redis miss 不恢复新旧 Pending，也不从 Message/摘要重建。

## 8. 测试与质量

| 检查 | 结果 |
| --- | --- |
| 全部单元测试 | 504 passed；新增四个文件共 104 项规划/澄清用例。 |
| interaction_robustness + task_react 集成 | 61 passed。 |
| 真实 PostgreSQL/Redis Conversation 集成 | 2 passed，无跳过。 |
| Ruff：本次 11 个新增/修改 Python 文件 | 通过。 |
| mypy：本次 5 个新增/修改生产 Python 文件 | 通过。 |
| git diff --check | 通过。 |

覆盖严格 JSON、完整 Schema 不放宽、extensions 禁止绕过、串行/并行图、所有引用和循环、
Catalog 状态、能力询问、条件、跨任务、保存和范围冲突、全计划失败不返回子计划、焦点歧义、
锁定任务防漂移、越权补槽原子拒绝、显式修正、TTL/owner/轮次/损坏/取消/替换，以及缓存和归档隔离。

零业务副作用测试对真实存在的 prepare_modify、prepare_full_rerun、prepare_initial_with_input、
execute_prepared、dispatcher.submit、Repository.create_execution 设置调用拦截，断言没有调用、
Execution 列表前后均为空。首轮测试桩曾引用不存在的 TaskCommands.execute，已改为以上真实入口。
真实 Conversation 集成仅通过测试进程读取本地连接配置，不输出连接串，不执行全局 Redis flush。

## 9. 未实现与架构边界

本次范围内无遗留项。未实现 NLP keyword parser、模型调用、ReAct/Tool 路由接入、专业 Tool、
Execution 创建、Compare Engine、Scenario Engine、条件循环、跨任务写执行、跨版本自动层匹配、
DependencyGraph、ImpactAnalyzer 或细粒度重跑；这些均不属于本 Task。

没有修改 W01–W10、数据库 Schema、migration、核心 Agent、A 的完整 Operation 模型或旧 Pending。
未修改 DEMO_SYSTEM_PROMPT、MockTaskShellModel._command、ALLOWED_TASK_TOOLS；没有新增 Tool。

Task 10.5-D 仍属于 planning infrastructure，尚未接入用户消息执行链；页面验收将在 10.5-E 完成后执行。
本次未进行人工页面测试，未修改前端或运行前端 build。

No Architecture Issue found.

完成后停止，不开始 Task 10.5-E 或 Task 11。

## D1 hardening follow-up

后续独立审查发现原 D 提交仍有四个安全边界需要定点加固：Active/View 冲突使用了无法由普通
task/execution patch 补齐的通用冲突槽；任务创建动作可能被要求提供已有 Task；PlanValidator 接受
裸 task_id 映射作为解析结果；Pending save 完全相信调用方 issues。Task 10.5-D1 在独立提交中修复
上述问题并重新执行测试。D1 记录见 `010-5d1-planning-safety-hardening.md`；本节替代“原 D 无遗留风险”
的绝对表述，不改变 D 的整体架构边界。
