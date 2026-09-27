# Task 10.5-E1：OperationPlan 到现有任务命令执行桥

## 定位与基线

E 分为 E1 的确定性执行桥和 E2 的 AgentScope ReAct / qwen-plus / Web 集成，
便于独立验证授权、选版和零副作用拒绝。E1 只接收程序化结构化计划，尚未改变页面路径。

- 开发分支：`codex/task-10-5-operation-understanding`。
- 开始 HEAD：`c5d96b46680a3b3db65c1caeb822408954c4888f`。
- 稳定基线：`origin/codex/demo-2026-10-31`，`ba727747c9a8a114364e40ce0650e679b1134bd5`。
- fetch、switch、pull --ff-only 后确认 A、B、C、D、D1、D2 均在历史中，ahead 7 / behind 0。
- 已有 `.idea/`、两份 demo_output JSON、`docs/gdsx-tool-function-inventory.md` 保留且不提交。
- 提交信息：`feat: bridge operation plans to task commands`；最终 SHA 见完成报告。不创建 PR。

## 动作与已有命令

新增 ActionType.FULL_RERUN（全量重跑已有任务），Capability 为 ENABLED（已启用），
仅支持 WHOLE_WELL（整井范围），handler_name 为 rerun_well_interpretation。
它保留当前有效参数，不携带局部范围、修改值、方法或模型选择。
REINTERPRET（按目标和范围重新解释）仍为 NOT_IMPLEMENTED（尚未实现），不得冒充全量重跑。
FULL_INTERPRET（首次解释）仍已启用，但本 Bridge 返回
INITIAL_INPUT_ROUTE_REQUIRED（首次解释需要输入资料入口），不创建 Task/InputVersion/Execution。
上传 → parse_upload → 已校验输入 → Task/InputVersion → run_well_interpretation 的稳定链不变。

| Operation | Command | 范围与参数 |
| --- | --- | --- |
| MODIFY_PARAMETER（修改计算参数） | ModifyInterpretationCommand | por、perm、sampling_interval、prediction_model；应用层决定专业重跑范围。 |
| FULL_RERUN（全量重跑） | FullRerunCommand | 仅已有授权 Task，整井、无修改参数。 |
| STATUS（查询状态） | GetStatusCommand | 仅 Task 当前 Execution。 |
| REPORT（查看报告） | GetReportCommand | 传入 B Resolver 解析出的 execution_id，读取已有报告。 |

adapter 显式白名单映射，不动态导入 handler，不调用 TaskCommandTool，不创建 Agent 或新业务 Tool。

## Pipeline 与授权

1. 从 runner.interaction_context 获取隔离快照，调用 OperationContextResolver 补候选引用。
2. 复用 PlanValidator.validate_graph 做结构预检，避免重复 operation_id 在解析映射中覆盖彼此。
3. 经 runner.context 获取当前持久模式对应的 Repository，调用 OperationReferenceResolver.resolve_task。
   只读由 READ_ACTIONS 决定 READ_ONLY（只读引用解析），其余采用 WRITE（写引用解析）。
4. 调用 resolve_execution；缺版本语义时使用 TASK_CURRENT（任务当前版本），匹配 Active 的写请求保留
   ACTIVE_BASE（当前工作基线）。PREVIOUS（上一版本）仅使用同 Task 的 Active 基线作锚点。
5. 未指定范围规范为整井；所有有目标的节点都调用 ScopeResolver，不能跨 Execution 使用旧 interval_id。
6. 将 typed ResolvedTaskReference 映射和 Context issues 整体交给 PlanValidator，验证整个计划。
7. 完整适配所有命令，检查版本、参数、保存意向和复合执行边界，然后再次调用 B Resolver 核验写引用。
8. 全部命令先通过 InteractionPolicy，再调用 runner.execute；写成功清除旧基线，读成功更新 View。

没有 Session Identity 时返回 OPERATION_SESSION_IDENTITY_REQUIRED（操作需要会话身份）。
所有 command.task_id 来自 B 返回的 ResolvedTaskReference，不能直接使用模型、Active 或 View 中的字符串。
Bridge result 保留最小 task/execution/scope 解析读模型和 TaskCommandResult，不暴露 state_snapshot。
无效结构与参数返回固定信息；Application 异常只透传稳定代码，不暴露原异常文本。
能力询问只返回 Validator 的能力事实，不查询或提交业务命令。

## 版本与并发安全

REPORT 默认选任务当前版本；上下文已经明确查看版本时尊重补全的引用。
显式 PREVIOUS / LATEST_SUCCESSFUL（最近成功版本）/ FIRST（首次版本）/
SEQUENCE（按序号指定）/ EXECUTION_ID（按明确 ID 指定）/ ACTIVE_BASE 全部经 B 解析。
REPORT 的 InteractionPolicy selector 使用 EXPLICIT（明确版本），避免按当前版本报告可用性错误阻断历史报告。
报告是否存在继续由 Application 返回 REPORT_NOT_FOUND（未找到报告）或 REPORT_NOT_READY（报告未就绪）。
STATUS 的最终引用若不是 TASK_CURRENT，返回
STATUS_EXECUTION_SELECTION_UNSUPPORTED（状态查询暂不支持历史版本选择）。

写操作解析出的版本必须等于 Task.current_execution_id，否则返回
HISTORICAL_BASE_WRITE_UNSUPPORTED（历史基线写入当前不支持）。当前 Active Base 与 current 相等则允许。
首次解析后至写入前任务选择或版本变化，返回 STALE_CONTEXT_REFERENCE（上下文引用失效）。
绑定撤销仍按 B 的 TASK_NOT_FOUND（任务不存在或未授权）拒绝。

Runner 取得命令锁后再调用 Bridge 的 before_write，重新核验 Binding、任务和版本，保护等锁期间的变化。
此外新增可选 expected_current_execution_id 并发前置条件，从 Runner 传给 TaskCommands.prepare_modify /
prepare_full_rerun；它必须等于 Application 新生成计划的 expected_current_execution_id，
否则返回 STALE_EXECUTION_PLAN（执行计划已失效）。随后 Repository 的原子 expected-current 检查继续生效。
这接续了 Bridge → Application → Repository 的版本检查；没有为 Command 增加 source/base 选择字段，
不支持历史分支写，也没有替换现有专业依赖规划或并发保护。

有 Session Identity 的 Runner 归属检查在内存与持久模式都以 Binding 为准，不能以 observed cache 授权；
无身份的旧 memory 测试仍保留原兼容入口。运行中修改/重跑仍受 InteractionPolicy 的
TASK_EXECUTION_ACTIVE（任务仍有活跃执行）与 Application 底层检查阻断。

## 参数与复合计划

优先使用 parameter_name，有限 Target 映射为 POROSITY（孔隙度）→ por、
PERMEABILITY（渗透率）→ perm、MODEL（预测模型）→ prediction_model。
井目标不能猜参数，sampling_interval 必须明确给名。Target 与参数矛盾时返回
INVALID_OPERATION_PLAN（操作计划结构非法）。模型修改要求 model_id，不伪造 float ValueSpec；
因此 missing_plan_slots 同步区分模型标识和数值缺槽，其余修改仍要求数值。

仅支持 ABSOLUTE（绝对值）；DELTA（绝对增量）和 PERCENT_CHANGE（比例变化）返回
UNSUPPORTED_VALUE_MODE（当前不支持该值变更模式）。数值原样传入 InterpretationOverride，不做单位换算，
POR 的 0～1 等约束仍由现有契约校验。method_id、额外 model_id 和未支持参数不能偷偷丢弃后执行。
extensions 可保存非正式元数据，但任意字段均不进入 Override/Command；影射 task_id 等正式字段仍由 Parser 拒绝。

同一已解析 Task、同一当前 Execution、整井的多个参数修改聚合为一次 ModifyInterpretationCommand，
只有一个新 Execution。重复参数同值去重，异值返回 MULTI_OPERATION_CONFLICT（复合操作存在冲突）。
其他复合写、修改 + 报告、修改 + 全量重跑返回
COMPOUND_EXECUTION_UNSUPPORTED（当前执行桥不支持该复合执行），在任何写前整体拒绝。
纯读计划先完成所有解析、校验、适配和 Policy，再按顺序边的稳定拓扑次序读取，不新增 Execution。
数据/比较依赖、额外 input_refs、无法保证的附加约束和预览写均拒绝，不默默忽略用户语义。
ONLY_SCOPE（仅限当前范围）可由已解析整井范围满足；专业 DependencyGraph 不在 Bridge 内实现。

## Context 与后续生命周期

- 读成功：set_view_context(result.task_id, result.execution_id, scope)，不调用 set_active_task，保留 Active Base。
- 写命令成功提交：set_active_task 后 clear_active_base，保留任务，清空 base_execution_id 和 scope；View/Recent 保留。
- clear_active_base 同时提供 Store 和 Runner facade；无 Active 时和重复调用均安全。
- 旧 TaskCommandTool 的路由及读后切 Active 行为保持原状，避免 E1 未接 ReAct 时出现半迁移。
- E1 不创建 OperationClarificationStore。E2 必须将其 owner/TTL/轮次生命周期绑定 Runner/Session，
  不能每次调用创建新 Store，也不能从聊天摘要恢复待执行写请求。

## 测试与交付

新增生产模块仅 operation_execution_bridge.py 与 operation_command_adapter.py；新增桥接单元测试文件。
修改 Operation Models/Catalog、Parser 的模型参数缺槽、Validator 的禁止全量重跑冲突、
InteractionContextStore、Runner、TaskCommands，以及相应四份已有单测和本任务的三份文档。

桥接测试使用真实 TaskSessionIdentity、SessionTaskBinding、InMemoryTaskRepository 和 TaskCommandRunner，
首次 Fixture 运行与后续修改均经过现有 Application/后台 Worker/Workflow，等待完成后确认真实报告。
覆盖全部报告选版、Active/View 隔离、参数聚合、重复冲突、目标不一致、相对值拒绝、模型标识缺失、
现有 Override 限制、FULL_RERUN 与 REINTERPRET 边界、历史写、所有局部 Scope、跨版本层段、
整计划预检、绑定撤销、版本漂移、Application 规划前的并发变化、缓存非授权及已有 Execution 活跃保护。
失败用例断言 Execution 数量和 Context 不变；并发注入用例只允许测试中的另一个写者新增一版。

最终验证结果：

| 检查 | 结果 |
| --- | --- |
| 指定的 Models、Catalog、Task/Scope Resolver、Context、Parser、Context Resolver、Validator、Clarification、Bridge 十个单测文件 | 378 passed |
| `uv run pytest -q tests/unit/` | 603 passed |
| `test_interaction_robustness.py` + `test_task_react.py` | 61 passed |
| Ruff：本次新增/修改的 14 个 Python 文件 | 通过 |
| mypy：本次新增/修改的 9 个生产 Python 文件 | 通过 |
| `git diff --check` | 通过 |

全 unit 包含 Conversation persistence 及 A～D2 回归。未运行外部 PostgreSQL/Redis 环境测试；
本次桥接业务执行验证使用真实内存 Repository、Binding、Application 与后台 Worker。

## 页面、未实现与 Architecture Issue

未修改 ReAct、DEMO_SYSTEM_PROMPT、MockTaskShellModel、ALLOWED_TASK_TOOLS 或前端，未新增业务 Tool。
未改变 W01–W10、专业算法、Task/Execution Repository 实现、数据库 Schema 或 migration。
E1 未进行人工页面测试；E2 必须进行真实 Web / AgentScope 页面验收。
本次未实现自然语言 → OperationPlan、ReAct/Streaming 集成、Compare/Scenario、局部重跑、历史分支写、
TVD 转换或人工结果覆盖；它们不属于 E1。范围内无遗留功能项，不开始 E2。

No Architecture Issue found.
