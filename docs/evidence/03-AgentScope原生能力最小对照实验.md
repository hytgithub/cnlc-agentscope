> **项目治理定位**：本文件已纳入 `cnlc-agentscope` 实施仓库。
> **治理状态**：`EVIDENCE`
> **来源**：`cnlc-interpretation-agent/docs/08-Task02-AgentScope原生能力最小对照实验.md`
> **迁入日期**：2026-10-04
> **说明**：AgentScope 原生能力实验设计与证据入口。
> **来源保真**：除本治理标识外，正文保持来源文档内容；正文中历史“初稿/讨论稿”等状态描述保留，用于反映形成过程。
# 08｜Task 02：AgentScope 原生能力最小对照实验

> 日期：2026-10-04  
> 状态：实验方案，待在可运行 AgentScope 2.0.8 的工作区执行  
> 目标：用同一组测井交互场景对比当前自定义实现与 AgentScope 2.0.8 原生能力，判断哪些职责可以回归框架，哪些必须继续由确定性业务代码保证。

关联文档：[06｜现状事实盘点](./01-测井解释智能体现状事实盘点.md) ｜ [07｜现有系统运行证据表](./02-现有系统运行证据表.md)

## 1. 为什么做 Task 02

Task 01 已经确认：

- 当前系统确实使用 AgentScope 2.0.8；
- 外层 ReAct 只暴露两个高层 Tool；
- Skill / MCP 被项目代码显式禁止；
- W01～W10、四阶段推进、Operation 解析、澄清、版本和权限主要由项目自定义代码保证；
- 三组领域核心最小运行验证已取得证据；
- 尚未验证 AgentScope Skill、原生任务规划、HITL、Tracing 等能力是否可以承担一部分智能决策职责。

Task 02 不开发最终业务功能，只回答：

> 现有自定义 Operation / 编排代码里，哪些属于 Agent 应该理解和决策的事情，AgentScope 2.0.8 原生能力能否稳定承担？

## 2. 已核实的 AgentScope 2.0.8 官方能力

### 2.1 Toolkit 原生支持 Skill

官方 v2.0.8 源码 src/agentscope/tool/_toolkit.py 中，Toolkit 构造函数直接支持 tools、skills_or_loaders、mcps，并注册内置 SkillViewer。

官方源码明确说明：Skill 不是 Tool；Agent 先读取 Skill 的完整说明，再按 Skill 指导使用已有 Tool / 资源。

因此：当前项目禁止 Skill 是项目自己的架构选择，不是 AgentScope 2.0.8 不支持。

### 2.2 AgentState 原生带 TaskContext

官方 v2.0.8 的 AgentState 包含 tasks_context；Task 工具包括 TaskCreate、TaskGet、TaskList、TaskUpdate。

这些 Task 可以用于复杂多步骤任务、任务依赖和进度跟踪，但它们是 Agent 会话任务，不是测井业务 Task / Execution。

不能用它替换：数据库业务任务、结果版本、Execution、当前有效解释版本。

### 2.3 HITL / 中断事件存在

官方 v2.0.8 事件系统包含 REQUIRE_USER_CONFIRM、USER_CONFIRM_RESULT、USER_INTERRUPT、REQUIRE_EXTERNAL_EXECUTION、EXTERNAL_EXECUTION_RESULT。

因此 AgentScope 原生具有人机确认与中断基础机制。

必须区分：中断 Agent ReAct 不等于取消已经提交给公司重 API 的真实任务。

### 2.4 TracingMiddleware 已存在

官方 v2.0.8 提供 TracingMiddleware，基于 OpenTelemetry 记录 reply、model call、tool execution、HITL / external execution。

它可以用于实验是否减少自定义 Agent 执行轨迹代码，但不能替代 ToolRun、Task / Execution、数据版本来源、StageRun 和业务审计。

### 2.5 GoalPipeline 已存在，但不是当前业务 Plan 的直接替代

官方 v2.0.8 的 GoalPipeline 是 Executor Agent 与 Verifier Agent 的循环，验证结果为 pass / fail / impossible。

它适合目标执行加验证闭环，但不是当前井业务版本规划器、参数影响依赖图、RUN / REUSE 四阶段计划或 OperationPlan 的天然替代品。

## 3. 实验总原则

### 3.1 不修改正式主业务链

Task 02 使用独立实验分支或目录，例如：

~~~text
branch: codex/task-12-agentscope-native-capability-poc

experiments/
  agentscope_native_poc/
~~~

实验阶段不得删除 OperationBridge、StageOrchestrator、Workflow、Context、版本服务或 ToolCaller。

### 3.2 所有实验使用同一组业务场景

统一输入至少包含：

1. 帮我解释这口井
2. 看看 2035–2038m
3. 看一下刚才那层
4. 把孔隙度改成 0.16
5. 把刚才那层孔隙度改成 0.16
6. 比较修改前后
7. 确认并继续
8. 先停一下
9. 刚才我说错了，改成 0.18
10. 切回上一口井
11. 只查看报告，不要重新算
12. 继续上一版的处理

局部修改、Compare 等当前业务能力尚未正式开放时，Mock 只能返回已识别但当前不支持，不能伪装成已经完成真实修改。

### 3.3 原生方案也必须经过确定性 Tool 校验

Agent 输出修改意图后仍必须经过受控 Tool，再由权限、Task、Execution、参数和版本校验决定能否真正执行。

## 4. 实验 E01：Prompt + 多业务 Tool 动态选择

### 目标

验证当前一个巨大 Operation Tool 是否有必要。

当前方案 A：

~~~text
Agent
  ↓
interpret_interpretation_operation
  ↓
OperationPlan
  ↓
Resolver / Validator / Bridge
~~~

原生实验 B：给 AgentScope Toolkit 暴露少量语义清晰的 Mock 业务 Tool：

- start_full_interpretation
- query_interpretation_result
- preflight_modify_parameter
- apply_parameter_change
- compare_result_versions
- read_report
- confirm_stage

Tool 不执行真实业务，只记录 Tool 名、参数、顺序和返回状态。

### 判定

| 指标 | A 当前方案 | B 原生 Toolkit |
|---|---|---|
| Tool 选对 | 待测 | 待测 |
| 参数正确 | 待测 | 待测 |
| 无额外 Tool | 待测 | 待测 |
| 含糊时不写 | 待测 | 待测 |
| 多轮上下文 | 待测 | 待测 |
| 实现复杂度 | 待测 | 待测 |

结论只允许 PASS、PARTIAL、FAIL。PASS 才考虑拆小 Operation Tool。

## 5. 实验 E02：Skill 是否可以承载专业操作方法

### 目标

验证哪些内容应该放 Prompt，哪些应该放 Skill，而不是继续堆到 System Prompt / Python 分支。

建议建立三个最小 Skill：

~~~text
skills/
├── full-interpretation/SKILL.md
├── query-and-compare/SKILL.md
└── modify-and-rerun/SKILL.md
~~~

full-interpretation Skill 只描述完整解释目标、四阶段含义、每阶段应取得什么业务结果、可用 Tool，以及缺失结果时不能编造。

modify-and-rerun Skill 描述：理解修改目标 → 确认井 / 版本 / 范围 → 调用预检 Tool → 根据 Tool 返回的真实影响范围决定下一步 → 执行 → 查询新版本。

Skill 不得自己决定 POR 一定影响 SW。真实依赖必须由正式业务规则返回。

### A/B

- A：长 System Prompt + Tool
- B：精简 System Prompt + Skill + Tool

比较 Tool 选择、Prompt 长度、专业步骤遗漏、规则幻觉以及修改方法说明是否需要改 Python。

## 6. 实验 E03：AgentState / 对话上下文与现有 InteractionContext 的边界

### 目标

验证 AgentScope State / conversation context 能否承担自然语言连续性，而不复制业务事实状态。

场景包括刚才那层、之前那口井、上一版等指代。

执行前仍必须调用权威 Resolver 获取 task_id、execution_id、interval_id 和 ownership。

通过标准：语言指代可以由 AgentScope Context 辅助；一旦 Resolver 发现版本过期或归属冲突，必须以 Resolver 为准。

目标边界：AgentScope Context = 语言记忆；业务 Context = 权威执行事实。

## 7. 实验 E04：原生 Task planning 能否减少自定义计划代码

使用 TaskCreate、TaskGet、TaskList、TaskUpdate，让 Agent 对复杂请求生成会话任务，例如：修改孔隙度 → 重新解释 → 比较结果。

重点验证：是否帮助多步骤任务、用户中途改变要求时是否能调整、是否减少自定义 OperationPlan 中仅负责任务组织的代码。

即使实验通过，也不能替代 ExecutionPlan RUN / REUSE、真实参数依赖、current execution 校验、版本归属和 Stage dependencies。

GoalPipeline 只做单独适用性实验，不用于控制 W01～W10。

## 8. 实验 E05：原生 HITL / Permission 能否承担交互确认

候选写 Tool：apply_parameter_change。

预期流程：预检 → 写 Tool 请求确认 → 未确认前无修改 → 用户确认后继续 → 拒绝后不执行。

即使 HITL 通过，AgentScope HITL 只负责用户同不同意；StageRun / Repository 仍负责哪个 Execution、哪个 StageRun、是否已经确认、是否存在并发冲突。

目标是减少 UI / Agent 交互胶水，不是删除业务状态。

## 9. 实验 E06：UserInterrupt 能否简化先停一下

测试 AgentScope 是否能结束当前 ReAct reply、保存 AgentState、下一轮恢复对话，并正确处理未完成 ToolCall。

必须验证反例：若公司 API 已经提交真实任务，Agent interrupt 后只能说明 Agent 已停止继续调用；除非公司接口真实支持取消，否则不能显示公司任务已取消。

## 10. 实验 E07：TracingMiddleware 与现有 Trace 的分工

给 POC Agent 配置 TracingMiddleware，并接本地 OpenTelemetry collector 或内存 exporter。

检查 reply、model_call、tool_call、参数、结果、token、session / reply 和 HITL。

| 信息 | AgentScope Trace | 现有业务审计 |
|---|---|---|
| Agent reply | 适合 | 不必重复 |
| model call | 适合 | 当前业务日志弱 |
| Tool call | 适合 | ToolRun 也有 |
| Tool 参数 | 有 | ToolRun 有受控快照 |
| task_id | 可加 attribute | 业务表必须保留 |
| execution_id | 可加 attribute | 必须保留 |
| stage_run_id | 可加 attribute | 必须保留 |
| 专业结果来源 | 不足 | 必须业务持久化 |
| 版本生效 | 不负责 | 必须业务服务 |

预期可能结果：用 TracingMiddleware 替换部分调试 Trace，但不删除 ToolRun / Execution / StageRun。

## 11. 实验 E08：GoalPipeline 适用性

只选择适合 executor-verifier 的场景，例如报告生成：Executor 生成报告，Verifier 检查 execution_id、必要章节以及是否出现无来源专业数值。

暂不用于四阶段推进、W01～W10、参数重算依赖和版本状态机。

## 12. 最小 Mock Tool 集

建议：

- get_current_context
- start_full_interpretation
- query_interpretation_result
- preflight_modify_parameter
- apply_parameter_change
- compare_result_versions
- read_report
- confirm_stage

Mock 中的 affected_stages 等依赖关系必须明确标记为 Task 02 行为实验规则，不是正式测井专业依赖。

## 13. 固定实验数据集

每个实验至少覆盖六类表达：标准、口语、省略指代、多意图、冲突/纠正、不支持能力。

同一场景需要多次真实模型调用。暂不预设 98% 等准确率，先获得实际基线。

## 14. 每轮实验保存的证据

必须保存：case_id、user_input、system_prompt_version、skill_version、available_tools、expected_action、actual_tool_calls、tool_arguments、tool_results、hitl_events、agent_final_response、business_state_before、business_state_after、pass/fail、failure_reason、token_usage、trace_id。

不保存模型完整隐藏推理。

## 15. A/B 决策规则

每个能力最终只能进入以下分类：

- KEEP：当前确定性代码继续保留。
- REPLACE：原生能力稳定承担，现有自定义代码可删除。
- WRAP：原生能力做上层智能决策，现有确定性服务继续做底层保障。
- ADD：AgentScope 和现有业务都没有，确实需要新增能力。

WRAP 很可能是本项目最常见的结果。

## 16. Task 02 退出标准

| 能力 | 原生实验结果 | 最终决策 |
|---|---|---|
| Prompt | 待测 | KEEP / REPLACE / WRAP |
| Toolkit 动态选择 | 待测 |  |
| Skill | 待测 |  |
| AgentState / Context | 待测 |  |
| Task planning | 待测 |  |
| HITL | 待测 |  |
| Interrupt | 待测 |  |
| Tracing | 待测 |  |
| GoalPipeline | 待测 |  |

最终输出《AgentScope 原生能力验证与代码决策矩阵》，随后才进入 Task 03 根据证据调整正式架构。

## 17. 不需要 Task 02 再争论的硬边界

以下内容不能因为某次 Agent 实验表现不错，就交给 LLM：

- 用户 / Session / Task 授权；
- Task / Execution / InputVersion 归属；
- 写前版本并发校验；
- Artifact / 文件来源；
- Tool 真实 API；
- 参数 Schema / 允许修改范围；
- 真实专业计算；
- 经确认的业务依赖；
- 结果完整性；
- Active 版本生效；
- 幂等；
- API timeout unknown 状态。

Task 02 要优化的是智能体如何理解、组织、选择、交互，而不是把不能错的业务规则改成模型猜。

## 18. 官方依据

AgentScope v2.0.8 官方源码：

- src/agentscope/tool/_toolkit.py：Toolkit、SkillViewer、skills_or_loaders
- src/agentscope/tool/_task/*：TaskCreate / TaskGet / TaskList / TaskUpdate
- src/agentscope/state/_state.py：AgentState / TaskContext
- src/agentscope/event/_event.py：HITL / interrupt / external execution events
- src/agentscope/middleware/_tracing/_trace.py：TracingMiddleware
- src/agentscope/pipeline/_goal_pipeline.py：GoalPipeline

本文件只使用 v2.0.8 能确认的能力作为实验基础。后续版本新增能力不得反向当成当前项目 2.0.8 已有能力。