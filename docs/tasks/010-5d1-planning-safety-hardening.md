# Task 10.5-D1：操作规划安全边界加固

## 1. 原因与基线

D1 是 Task 10.5-D 后的定点修复，不是架构重构。Codex 完成报告和任务文档中的测试数字只作为记录，
不是唯一验收依据；本次在改动前从实际代码重新运行目标测试，并在改动后重新执行完整回归。

- 分支：`codex/task-10-5-operation-understanding`。
- D 原提交 / D1 开始 HEAD：`349a193729554f5b6df7f8312bf996fc1c1b035a`。
- 稳定基线：`origin/codex/demo-2026-10-31`，HEAD
  `ba727747c9a8a114364e40ce0650e679b1134bd5`。
- 开始时开发分支相对稳定基线 ahead 5 / behind 0，已跟踪工作区干净。
- 原有未跟踪 `.idea/`、两份 `demo_output` JSON 和 `docs/gdsx-tool-function-inventory.md`
  保留且不纳入提交。
- D1 提交：`fix: harden operation planning safety boundaries`；SHA 见完成报告。

改动前实际运行四个规划测试文件：`104 passed`。该数字来自本次命令执行，不复制 D 文档。

## 2. Active / View 冲突可澄清

OperationContextResolver 仍返回稳定错误 `VIEW_ACTIVE_CONTEXT_CONFLICT`，但把问题定位到真正可补字段：

| 场景 | ClarificationSlot（澄清槽位） | 普通回复 |
| --- | --- | --- |
| Active=A，View=B/V1，隐式写 Task | `TASK`（任务对象） | `ClarificationPatch.task_reference` 可明确 A 或其他任务。 |
| Active=A 且无 base，View=A/V1，隐式写版本 | `EXECUTION`（版本对象） | `execution_reference` 可明确 TASK_CURRENT 或 V1。 |

普通 `CLARIFICATION_REPLY`（澄清回复）保存、补槽、重新经过 Context Resolver 与 PlanValidator 后，
两种冲突都可解除，不需要伪装成 `CORRECTION`（修正上一输入），也不会得到
`CLARIFICATION_SLOT_INVALID`（澄清槽位修补非法）。端到端测试覆盖这两轮完整链路，仍不调用 Tool。

`CONFLICT_RESOLUTION`（冲突选择）继续保留给无法映射到单一结构字段的冲突。例如
ONLY_SCOPE（仅限指定范围）与 EXCLUDE_SCOPE（排除指定范围）的关系无法确定时，用户可能需要重新
描述实际范围；它没有变成允许修改任意字段的万能槽。

## 3. Task 创建动作边界

`TASK_CREATION_ACTIONS` 集中定义 `FULL_INTERPRET`（首次/整井新井解释）和
`NEW_WELL`（新井资料）。Context Resolver 不为它们继承 Active 的任务、版本或范围；
PlanValidator 不因缺少 TaskReference 返回 TASK 澄清。

- FULL_INTERPRET + WELL + WHOLE_WELL + CREATE_VERSION 在默认 Catalog 中获得 EXECUTABLE
  （可进入写处理）的规划资格。该结果不证明有附件，不创建 Task/InputVersion/Execution；附件校验、
  InputVersion 与 Task 创建仍由 10.5-E 或现有稳定 Tool 链负责。
- NEW_WELL 无已有 Task 时不产生 TASK 缺槽，但默认 Catalog 状态仍是 NOT_IMPLEMENTED（尚未实现），
  因此返回 KNOWN_UNSUPPORTED（已识别但当前不支持）。D1 没有改变 Capability 状态。
- 两种创建动作若显式携带已有 TaskReference，返回 INVALID_OPERATION_PLAN（操作计划结构非法），
  防止把它们解释为已有任务重跑；已有 Task 的全量重跑继续使用稳定 FULL_RERUN 链。

首次附件解释的确定性链没有修改：上传附件 → 创建 Task/InputVersion → run_well_interpretation →
W01–W10。D1 没有接附件、DemoAgent、上传路由或 Task Tool。10.5-E 集成时，首次附件仍应走该稳定链，
不能因没有 Active Task 进入“先选择任务”的澄清。

## 4. typed 解析结果边界

PlanValidator 的参数由 `resolved_task_ids: Mapping[str, str]` 收紧为
`resolved_task_references: Mapping[str, ResolvedTaskReference]`。Validator 只读取 Task 10.5-B
读模型的 task_id；运行时发现裸字符串或其他类型会抛 TypeError，不提供字符串兼容入口，也不新增
重复的可信引用模型。

ResolvedTaskReference 只表示 B Resolver 在某一时刻的解析事实，不是永久授权或授权 token。
10.5-E 真正写入前仍须重新检查 SessionTaskBinding、Task/Execution ownership 和 Application
并发/状态。没有新增 `trusted=True` 或绕过 Repository 的捷径。

## 5. Pending issue 自校验

OperationClarificationStore.save 在写入 runtime 前重新调用 `missing_plan_slots(partial_plan)`，将
Schema 级缺口与 Context/Reference 调用方 issues 合并。稳定去重键为
`(operation_id, slot, error_code)`；调用方 issue 先加入，因此相同键保留调用方更具体的 message/evidence，
随后只补缺失的 Schema issue。

这保证 target=None 且 caller issues 为空时仍保存 TARGET（操作目标）问题，下一轮普通 target patch
可以成功。调用方已有同一 TARGET 时最终只保留一条。计划完整且 caller issues 也为空时，save 以现有
ClarificationPatchError 受控拒绝调用方误用，不新增稳定错误码，也不创建空问题 Pending。
Context/Reference 的 TASK、EXECUTION、SCOPE 或冲突 issues 仍会保留，不会被 Schema 检查覆盖。

## 6. 测试与边界

改动后目标四文件测试：`117 passed`；完整 unit：`517 passed`；
interaction_robustness + task_react 集成：`61 passed`。Ruff、3 个修改生产文件的 mypy 和
`git diff --check` 全部通过。新增覆盖：两种完整两轮冲突澄清、两个 Task 创建动作、
显式旧 Task 拒绝且能力询问仍只读、typed B 结果及裸字符串拒绝、Pending 自动补槽/去重/空问题拒绝。

Whole-plan first、then execute 原子性没有改变。D1 没有调用 create_execution、prepare_modify、
prepare_full_rerun、prepare_initial_with_input、execute_prepared、dispatcher.submit 或 rerun_planned。
没有修改 Capability 业务状态、旧 PendingClarification、Conversation sanitizer、数据库 Schema、
migration、W01–W10、专业算法、曲线数据、前端、DEMO_SYSTEM_PROMPT、MockTaskShellModel、
ALLOWED_TASK_TOOLS 或 ReAct/Tool routing，也没有新增业务 Tool。

未实现 Task 10.5-E 的模型结构化输出、附件/Planner 集成和实际执行映射。本次没有用户可见行为，
不进行人工页面测试；完整页面验收仍在 10.5-E 后执行。

## 7. Architecture Issue

No Architecture Issue found.

完成后停止，不开始 Task 10.5-E。
