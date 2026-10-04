# 测井解释智能体文档导航

本目录同时保存项目主设计基线、当前实现说明、证据、历史差距分析、Task 执行记录和验收基线。阅读时必须先区分“目标设计”和“当前实现”：

- **目标与当前认可的设计方向**：以 [design/](design/) 为准；
- **为什么这样设计、哪些结论已被验证**：以 [evidence/](evidence/) 为准；
- **代码现在实际上怎么工作**：以代码、Alembic migration 和下方 Current Implementation 文档为准；
- **一次具体怎么实施**：以 [tasks/](tasks/) 为准。

状态、枚举、动作码和流程节点的中文含义统一见 [11-status-enum-glossary.md](11-status-enum-glossary.md)。

## 文档同步规则

本项目实行“**代码与文档同步完成**”原则：

> 每次代码、配置、架构、接口、状态、测试或用户行为发生变化，都必须在同一 Task 中检查并完善受到影响的文档。

不要求机械修改全部五份主设计文档，而是按影响范围更新：

- 需求变化 → `docs/design/01`
- 目标变化 → `docs/design/02`
- 用户效果变化 → `docs/design/03`
- 技术方案变化 → `docs/design/04`
- 测试 / 验收变化 → `docs/design/05`
- 新实验 / 新证据 → `docs/evidence/`
- 当前实现变化 → 对应 Current Implementation 文档
- 状态 / 枚举变化 → `docs/11-status-enum-glossary.md`
- 单次实施结果 → `docs/tasks/`

如果代码已经改变而相关文档没有同步，该 Task 不算完成。详细规则见根目录 `AGENTS.md`。

## 项目主设计基线（Project Design Baseline）

| 阶段 | 文档 | 治理状态 |
| --- | --- | --- |
| 需求 | [design/01-测井解释智能体需求规格说明.md](design/01-测井解释智能体需求规格说明.md) | BASELINE |
| 目标 | [design/02-测井解释智能体建设目标.md](design/02-测井解释智能体建设目标.md) | BASELINE |
| 效果 | [design/03-测井解释智能体预期效果.md](design/03-测井解释智能体预期效果.md) | BASELINE |
| 方案 | [design/04-测井解释智能体技术方案.md](design/04-测井解释智能体技术方案.md) | VALIDATING |
| 测试 | [design/05-测井解释智能体测试与验收方案.md](design/05-测井解释智能体测试与验收方案.md) | VALIDATING |

治理规则见 [design/README.md](design/README.md)。

## 证据（Evidence）

- [evidence/01-测井解释智能体现状事实盘点.md](evidence/01-测井解释智能体现状事实盘点.md)
- [evidence/02-现有系统运行证据表.md](evidence/02-现有系统运行证据表.md)
- [evidence/03-AgentScope原生能力最小对照实验.md](evidence/03-AgentScope原生能力最小对照实验.md)
- [evidence/04-Task012A-Prompt-Toolkit-Skill实验结果.md](evidence/04-Task012A-Prompt-Toolkit-Skill实验结果.md)
- [evidence/05-Task012A1-Skill稳定性与Grounding实验结果.md](evidence/05-Task012A1-Skill稳定性与Grounding实验结果.md)

证据用于支撑技术方案和验收方案的修订，不自动改写需求、目标和预期效果。详见 [evidence/README.md](evidence/README.md)。

## 当前实现说明（Current Implementation）

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
| Conversation 持久化 | [12-conversation-persistence.md](12-conversation-persistence.md) | PostgreSQL 长期会话、Redis TTL、恢复和删除语义 |
| 流式进度与 UI | [09-streaming-progress-and-ui-design.md](09-streaming-progress-and-ui-design.md) | 首次解释 SSE、Telemetry 投影和界面边界 |

## 历史差距分析（Historical Gap Analysis）

- [06-interactive-agent-gap-analysis.md](06-interactive-agent-gap-analysis.md) 是实施前后的历史代码扫描和迁移计划，用于解释演进背景。
- 其中 `MISSING`、后续 Task 和建议实施顺序属于当时判断，**不是当前最终架构或当前能力清单**。

## Task 执行记录（Task Execution Record）

`docs/tasks/` 保存每个 Task 的实现与验证记录。当前前后端和首次解释流式联调记录见 [tasks/010-frontend-backend-integration.md](tasks/010-frontend-backend-integration.md)。执行记录说明当时环境与结果，不替代 Current Design。Task 10.3 见 [tasks/010-3-interaction-robustness.md](tasks/010-3-interaction-robustness.md)，Task 10.4 见 [tasks/010-4-conversation-persistence.md](tasks/010-4-conversation-persistence.md)。Task 012 的原生能力实验与职责决策见 [tasks/012-agentscope-native-capability-poc.md](tasks/012-agentscope-native-capability-poc.md)、[Task 012A.1](tasks/012a-1-skill-stability-grounding-poc.md)、[Task 012B](tasks/012b-multiturn-context-authority-poc.md) 和 [Task 012E 阶段性职责边界](tasks/012e-agentscope-native-final-responsibility-boundaries.md)。

## 验收（Acceptance）

- [mvp-acceptance.md](mvp-acceptance.md)：MVP 验收口径。
- 自动化测试和真实 PostgreSQL / Redis 联调结果应与对应 Task 记录一起阅读。

## 当前能力状态（Task 01～10.4）

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
| Durable Conversation / Redis Session TTL / Cache Restore | 已实现 |
| fixture / company_mock 四批次能力 | 已实现（Mock） |
| Heavy Prediction API | Future / 未实现 |
| Curve Visualization | Future / 未实现 |
| Fine-grained Capability | Future / 未实现 |

“Fine-grained Capability”包括 Capability Registry、依赖图以及 SW-only 等步骤级重算能力。当前局部重跑只从受支持的阶段边界开始。
