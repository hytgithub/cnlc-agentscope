# Task 11C — Stage Orchestrator

## 1. 目标与边界

`StageOrchestrator` 在现有连续执行之外提供
`STAGED_CONFIRMATION`（分阶段确认执行）。它只编排既有四阶段，不改变 W01～W10 的顺序、
节点算法、Agent 职责或报告装配逻辑。

两种持久化运行模式如下：

| ExecutionRunMode | 行为 |
| --- | --- |
| `CONTINUOUS`（连续执行） | 原入口一次运行 W01～W10 和报告；每个 StageRun 由系统自动确认。 |
| `STAGED_CONFIRMATION`（分阶段确认执行） | 一次只运行一个阶段；成功产物进入等待确认，确认后继续同一个 Execution。 |

四阶段映射保持固定：

| Stage | 执行范围 |
| --- | --- |
| `DECODE` / `DATA_DECODE`（数据准备） | W01 |
| `PREPROCESS`（预处理） | W02～W03 |
| `INTERPRET`（解释） | W04～W10 |
| `REPORT`（报告） | ReportAssembler |

## 2. Workflow 复用

`InterpretationWorkflow.run()` 继续从合法 `start_step` 一次运行至 W10，并使用
`auto_confirm=True`。`run_stage()` 调用同一个 `_run_steps()` 节点循环，只把范围限制到当前阶段，
并使用 `auto_confirm=False`。precondition、Tool/Agent 调用、StatePatch 合并、Telemetry、异常分类和
checkpoint 只有一套实现。

`run_stage()` 根据 `completed_steps` 校验完整连续前缀，并拒绝目标步骤已经出现在本 Execution
执行记录中的请求，防止静默重跑。REPORT 不伪装成 Workflow 节点，由应用层调用既有
`ReportAssembler`。

## 3. 确定性推进

下一阶段只由 `Execution.start_step` 和当前 Execution 自己创建的 StageRun 决定：

| start_step | 首阶段 |
| --- | --- |
| `W01` | DECODE |
| `W02` | PREPROCESS |
| `W04` | INTERPRET |
| `None` | REPORT |

进度计算始终过滤 `run.execution_id == execution.execution_id`。状态快照中继承自历史
Execution 的 StageRun 只用于依赖引用，不会被当成本次已执行阶段。

## 4. 暂停事务

一个阶段成功后，StageRun 从 `RUNNING`（执行中）进入
`WAITING_CONFIRM`（成功产出，等待确认）。Repository 在同一锁或 PostgreSQL 事务中：

1. 保存完整 InterpretationState；
2. 保存 REPORT 候选 Markdown（仅 REPORT 阶段）；
3. 把 Execution 从 `RUNNING` 改为 `WAITING_CONFIRMATION`；
4. 清空 `lease_owner` 和 `lease_expires_at`；
5. 更新 Task 当前 snapshot、status 和候选 markdown；
6. 保持 `current_execution_id`、sequence、首次 `started_at`，且 `finished_at` 为空。

`WAITING_CONFIRMATION` 不是终态，也不是有效 lease 的活动 Worker。过期 lease 恢复只扫描
`RUNNING`，不会终结等待用户确认的 Execution。

## 5. 确认事务

确认接口显式接收 `task_id`、`execution_id`、`stage`、`expected_stage_run_id` 和 `actor`。
Repository 在行锁内重新验证：

- Task 和 Execution 的归属；
- Task 当前指针仍指向该 Execution；
- Execution 正在等待确认；
- StageRun 同时匹配 task、execution、stage 和预期 ID；
- StageRun 是 `WAITING_CONFIRM/CURRENT`。

非报告阶段确认把 StageRun 改为 `CONFIRMED`（已确认），再把同一 Execution 原子重排为
`QUEUED`。它不创建新 Execution，也不清空首次 `started_at`。REPORT 确认在同一事务中写入
StageRun 确认、`SUCCESS/WARNING` 终态、`finished_at` 和最新成功指针。

`expected_stage_run_id` 防止旧页面按钮或并发双击误确认后来产生的 StageRun。新业务版本替换
Task 当前指针后，旧等待 Execution 的确认会以 `EXECUTION_NOT_CURRENT` 拒绝，历史快照不变。

## 6. 报告与进度投影

REPORT 等待确认期间，候选 Markdown 只通过内部 `StageProgress.candidate_report` 提供。
现有正式历史报告 API 仍只接受 Execution 终态。`StageProgress` 只返回阶段标识、状态、时间、
摘要和告警，不返回 RawData、完整曲线或本地路径。

## 7. 持久化恢复与多井隔离

`run_mode`、Execution 状态和 StageRun 全部持久化。应用重启后只需用明确的 `task_id` 与
`execution_id` 读取 PostgreSQL，即可确认并继续下一阶段。编排器不依赖 ActiveContext、Redis
或进程内会话状态，因此多口井暂停和恢复互不影响。
