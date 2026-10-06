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

### 2.0.8 Task API 版本事实补充（Task 012E 修订）

以上能力列表保留为 Task 012 建立时的历史调查记录。后续复核发现必须区分两类事实：当前项目 `.venv` 的 Task 012E 扫描当时没有定位到 `TaskCreate`、`TaskGet`、`TaskList`、`TaskUpdate`；但 AgentScope 官方仓库 `v2.0.8` tag 的 `src/agentscope/tool/_task/` 明确包含并导出这些会话 Task Tools。因此不能把“本地扫描未找到”写成“官方 2.0.8 不存在”。后续如验证原生 Task/Plan，必须先在当前实际安装环境做 import、Toolkit 注册和真实调用验证。无论这些 Tools 是否可用，它们都不等于本项目带授权、版本和持久化语义的业务 Task / Execution。详见 [Task 012E](012e-agentscope-native-final-responsibility-boundaries.md)。

## 子任务

1. 012A：Prompt + Toolkit + Skill 动态工具选择 POC（已执行）
2. 012A.1：Skill 稳定性、重复运行与 Response Grounding（已执行）
3. 012B：真实多轮 Context 与权威业务锚定边界（已执行；原始三次运行证据保留）
4. 012B.1：已补齐 M01～M06 第 4、5 次、保留 M07 三次，并更新 Evidence 06、04/05；012B 补齐闭环完成
5. 012C：AgentScope 原生 Task Tools 多步骤对照（已执行；Evidence 07；`REGISTERED_BUT_UNUSED（已注册但未使用）`）
6. 012C.1：写范围语义与生产安全边界核对（已完成；按结论 B 做了最小生产安全修复并记录 Evidence 08；参数天然粒度仍待业务确认）
7. 012C.2：Context Compression 当前输入与历史摘要边界（已完成；Evidence 09；`WRAP`，仅在本轮生命周期内保真当前输入，摘要不成为 Authority）
8. 012C.2 评审通过后再决定：HITL/Interrupt、TracingMiddleware（尚未实施）
9. 012E：当前阶段职责边界审计（已形成阶段性结论，不视为最终架构定稿）

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
