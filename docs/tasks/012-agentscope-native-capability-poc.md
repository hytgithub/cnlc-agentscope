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

## 子任务

1. 012A：Prompt + Toolkit + Skill 动态工具选择 POC
2. 012B：AgentState / Context + 原生 Task planning 对照
3. 012C：HITL / Permission / Interrupt 对照
4. 012D：TracingMiddleware 与现有 ToolRun / Execution Trace 对照
5. 012E：汇总 AgentScope 原生能力验证与代码决策矩阵

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