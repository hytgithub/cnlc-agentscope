# Task 012｜AgentScope 原生能力最小对照实验

> 分支：codex/task-12-agentscope-native-capability-poc
> 基线：codex/task-11-four-stage-execution @ a788e8eef116bbccead8e1d9748753599208b55d
> 类型：实验 / 证据任务，不改正式业务架构

## 目标

使用项目锁定的 AgentScope 2.0.8，对当前自定义交互与编排代码做原生能力 A/B 实验，回答哪些职责可以由 AgentScope 原生能力承担。

本 Task 不直接重构生产代码，不删除 Operation / Workflow / Stage / Context / Version 等现有实现。

## 已确认的 AgentScope 2.0.8 官方能力

- Toolkit 支持 tools、skills_or_loaders、MCP；
- Skill 通过内置 SkillViewer 供 Agent 按需读取；
- AgentState 包含任务上下文，官方有 TaskCreate / TaskGet / TaskList / TaskUpdate；
- Event 支持 RequireUserConfirm / UserConfirmResult / UserInterrupt / ExternalExecution；
- TracingMiddleware 支持 OpenTelemetry；
- GoalPipeline 支持 Executor + Verifier 循环。

不得把 GoalPipeline 或 AgentState Task 直接视为现有 OperationPlan / ExecutionPlan 的替代品，必须经过对照实验。

### 2.0.8 本地源码核验补充（Task 012E）

以上能力列表保留为 Task 012 建立时的历史调查记录。Task 012E 对当前仓库 `.venv` 内安装的 AgentScope 2.0.8 源码再次核验：可见 `TaskContext` 和通用 `Task` 数据对象，但没有找到 `TaskCreate`、`TaskGet`、`TaskList`、`TaskUpdate` CRUD API 或同名内置 Tools。因此“有 Task 上下文数据模型”已核实，“官方提供上述 Task CRUD API”在本安装版本中未核实，不得据此替代项目 OperationPlan、ExecutionPlan、StageRun 或业务持久化。详见 [Task 012E](012e-agentscope-native-final-responsibility-boundaries.md)。本说明补充版本证据，不改写当时实验事实。

## 子任务

1. 012A：Prompt + Toolkit + Skill 动态工具选择 POC（已执行）
2. 012A.1：Skill 稳定性、重复运行与 Response Grounding（已执行）
3. 012B：真实多轮 Context 与权威业务锚定边界（下一步）
4. 后续按证据决定：原生 Task/Plan 对照、HITL/Interrupt、TracingMiddleware
5. 012E：当前阶段职责边界审计（已形成阶段性结论，不视为最终架构定稿）

## 硬边界

以下内容不进入替代实验：

- Session / Task 权限；
- Task / Execution / InputVersion；
- 写前并发和 stale version 校验；
- Artifact / GDSX；
- 真实 API Adapter；
- 参数 Schema 与正式可修改范围；
- 专业计算与经业务确认的依赖；
- 结果生效与版本一致性；
- 幂等与外部 API unknown 状态。

## 总体退出条件

每项能力必须有同一测试集下的 A/B 证据，并明确归类 KEEP / REPLACE / WRAP / ADD。没有测试证据不得删除现有代码。
