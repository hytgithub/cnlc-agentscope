# Task 10.5-D2：已解析任务引用一致性保护

## 背景与基线

D1 将 PlanValidator 的解析输入收紧为 `Mapping[str, ResolvedTaskReference]`，但 typed value 本身仍不足以
证明 mapping key 属于计划，也不能防止显式 TASK_ID 与 Resolver 结果相互矛盾。D2 只保护 Validator
接收 B 结果后的内部一致性，不修改 OperationReferenceResolver。

- 分支：`codex/task-10-5-operation-understanding`。
- D2 开始 HEAD / D1 commit：`3ef948fcc8306b8b22f8baafbe4bffc56f5a1e30`。
- 稳定基线 `origin/codex/demo-2026-10-31`：
  `ba727747c9a8a114364e40ce0650e679b1134bd5`。
- 开始时开发分支 ahead 6 / behind 0，已跟踪工作区干净；既有无关未跟踪文件保留。
- D2 commit：`fix: enforce resolved task reference consistency`；SHA 见完成报告。

## 两个 invariant

PlanValidator 在图、能力、缺槽和冲突裁决前检查：

1. `resolved_task_references` 的每个 key 必须属于当前计划的 operation_id。任一 unknown key，包括
   valid + ghost 混合 mapping，都会让整个调用抛出 ValueError；不静默忽略。
2. 节点自身或回退到 shared_context 的最终引用若为 `TASK_ID("A")`，对应
   ResolvedTaskReference.task_id 必须严格等于 A。不一致抛出 ValueError；不选择任一值，也不自动修正。

这两类问题表示 10.5-E 集成接线错误，采用 fail fast，不返回 NEED_CLARIFICATION、
KNOWN_UNSUPPORTED 或 ClarificationIssue，也不新增用户可见稳定错误码。

`WELL_ID("WELL_A")` 可正常解析为 `task-123`；Validator 不比较井号字符串和 task_id。
CURRENT（当前任务）与 PREVIOUS_TASK（上一任务）同样保持 symbolic reference 语义，接受 B 的实际选择。
节点显式引用继续优先于 shared_context。`Mapping[str, str]` 仍先因 value 类型错误抛 TypeError，
没有恢复兼容入口。

ResolvedTaskReference 是某一时刻的解析事实，不是永久授权。真正写入前仍须重新验证 Binding、
Task/Execution ownership 与 Application 并发和状态。

## 测试与边界

`test_plan_validator.py` 新增并覆盖：显式 TASK_ID 一致、节点不一致、shared TASK_ID 不一致、
WELL_ID 映射到不同 task_id、CURRENT、PREVIOUS_TASK、unknown key、valid + ghost 整体失败；
D1 的裸字符串 TypeError 测试继续保留。测试均只调用纯 Validator，业务副作用为零。

验证结果：PlanValidator 单文件 55 passed；四个 D 规划测试文件 125 passed；全 unit 525 passed；
interaction 关键回归 61 passed。Ruff、mypy 与 `git diff --check` 均通过。

D2 没有修改 Operation Schema、Capability Catalog、Context Resolver、Clarification、Pending、ReAct、
Tool、Application、Repository、Conversation、数据库 Schema、migration、W01–W10 或前端。
没有创建 Execution。未实现内容仍是 Task 10.5-E 的实际集成；本次不进行页面测试。

## Architecture Issue

No Architecture Issue found.

完成后停止，不开始 Task 10.5-E。
