# 测井解释智能体文档导航

本目录同时保存当前设计、历史差距分析、Task 执行记录和验收基线。阅读时应先确认文档类型；当前行为以代码、Alembic migration 和标为 **Current Design** 的文档为准。状态、枚举、动作码和流程节点的中文含义统一见 [11-status-enum-glossary.md](11-status-enum-glossary.md)；Current Design 文档首次出现这些代码值时应同步给出中文说明。

## 当前设计（Current Design）

| 主题 | 文档 | 说明 |
| --- | --- | --- |
| 项目背景 | [00-project-context.md](00-project-context.md) | 目标、范围与核心原则 |
| 业务 Workflow | [01-business-workflow.md](01-business-workflow.md) | W01～W10 业务顺序 |
| Agent / Tool 边界 | [02-agent-tool-boundary.md](02-agent-tool-boundary.md) | Agent、Workflow、Tool、Algorithm 的职责 |
| 总体系统架构 | [03-system-architecture.md](03-system-architecture.md) | 分层、运行时与基础设施总览 |
| 数据与 Tool Contract | [04-data-tool-contracts.md](04-data-tool-contracts.md) | 业务数据和专业工具契约 |
| 交互式总体架构 | [04-interactive-agent-architecture.md](04-interactive-agent-architecture.md) | ReAct 交互与确定性执行边界 |
| 交互式详细设计 | [05-interactive-agent-detailed-design.md](05-interactive-agent-detailed-design.md) | Task、Execution、规划和读模型 |
| 持久化运行设计 | [05-persistence.md](05-persistence.md) | PostgreSQL、Redis、事务、租约与恢复 |
| 数据库设计 | [07-database-design.md](07-database-design.md) | ER、版本、ToolRun、Binding 和并发控制 |
| 意图与交互设计 | [08-intent-and-interaction-design.md](08-intent-and-interaction-design.md) | 附件路由、ReAct、五个任务级 Tool |
| 交互状态机 | [10-interaction-state-machine.md](10-interaction-state-machine.md) | 异常交互、集中 Policy、短期澄清和状态裁决 |
| 状态与枚举中文词典 | [11-status-enum-glossary.md](11-status-enum-glossary.md) | Workflow、Execution、Validation、交互、ToolRun、规划等代码值的中文含义 |
| 流式进度与 UI | [09-streaming-progress-and-ui-design.md](09-streaming-progress-and-ui-design.md) | 首次解释 SSE、Telemetry 投影和界面边界 |

## 历史差距分析（Historical Gap Analysis）

- [06-interactive-agent-gap-analysis.md](06-interactive-agent-gap-analysis.md) 是实施前后的历史代码扫描和迁移计划，用于解释演进背景。
- 其中 `MISSING`、后续 Task 和建议实施顺序属于当时判断，**不是当前最终架构或当前能力清单**。

## Task 执行记录（Task Execution Record）

`docs/tasks/` 保存每个 Task 的实现与验证记录。当前前后端和首次解释流式联调记录见 [tasks/010-frontend-backend-integration.md](tasks/010-frontend-backend-integration.md)。执行记录说明当时环境与结果，不替代 Current Design。Task 10.3 见 [tasks/010-3-interaction-robustness.md](tasks/010-3-interaction-robustness.md)。

## 验收（Acceptance）

- [mvp-acceptance.md](mvp-acceptance.md)：MVP 验收口径。
- 自动化测试和真实 PostgreSQL / Redis 联调结果应与对应 Task 记录一起阅读。

## 当前能力状态（Task 01～10.3）

| 能力 | 状态 |
| --- | --- |
| Versioned Execution | 已实现 |
| Input Version | 已实现 |
| Partial Rerun | 已实现 |
| ToolRun | 已实现 |
| Interactive Commands | 已实现 |
| Background Execution | 已实现 |
| Execution Visualization | 已实现 |
| Session Binding | 已实现 |
| Frontend / Backend E2E | 已实现 |
| START / MODIFY / FULL_RERUN Streaming | 已实现 |
| Multi-well Session / Active Task Resolver | 已实现 |
| Interaction State / Policy / Pending Clarification | 已实现 |
| fixture / company_mock 四批次能力 | 已实现（Mock） |
| Heavy Prediction API | Future / 未实现 |
| Curve Visualization | Future / 未实现 |
| Fine-grained Capability | Future / 未实现 |

“Fine-grained Capability”包括 Capability Registry、依赖图以及 SW-only 等步骤级重算能力。当前局部重跑只从受支持的阶段边界开始。
