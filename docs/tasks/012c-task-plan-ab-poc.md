# Task 012C｜多步骤 ReAct vs AgentScope Task/Plan A/B POC

> 父任务：Task 012 / 012A / 012A.1 / 012B / 012B.1
> 分支：`codex/task-12-agentscope-native-capability-poc`
> 类型：AgentScope 2.0.8 隔离 POC / 多步骤规划能力 A/B
> 日期：2026-10-04
> 推荐执行模型：GPT-6 Luna
> 原则：只验证 Plan 是否带来真实稳定性增益；不修改生产代码；不把 AgentScope Task 当业务 Task/Execution。

## 1. 背景

Task 012B.1 已关闭 Context 方向的主要问题，并形成阶段性边界：

- Conversation Context：`WRAP`，只负责语言连续性和候选指代；
- Authority Context / Resolver：`KEEP`，负责 task / execution / scope / version 的权威绑定；
- Write Safety：仍由确定性 Validator / Policy / Tool Contract 保障。

当前剩余最明显的 Agent 能力缺口不是 Context，而是**多步骤连续执行稳定性**。

已有两组真实失败证据：

### 证据 A：Task 012A.1 Case 14

用户意图：

```text
修改参数
→ 查询新版本
→ 比较修改前后
```

结果：

```text
no_skill   0/5
with_skill 0/5
```

两组都没有稳定完成预期步骤。

### 证据 B：Task 012B / 012B.1 M03

用户意图：

```text
切到 WELL-B
→ 回到刚才那口井 WELL-A
→ 再查询刚才那层
```

native-context 五次都能切回 WELL-A，但：

```text
5/5 均未在同轮继续执行后续查询
```

这说明当前 ReAct 在“第一步做对后继续完成后续依赖步骤”方面仍不稳定。

本 Task 只回答：

> **AgentScope 原生 Task/Plan 能否显著改善多步骤执行完整性，同时不破坏 Authority Binding、Write Safety 和 Grounding？**

## 2. 关键边界

### 2.1 AgentScope Task/Plan 是什么

它只用于：

- 组织当前对话请求的多步骤工作；
- 记录“准备做什么 / 做到哪一步”；
- 帮助 Agent 不漏掉后续步骤。

它不是：

- 本项目业务 Task；
- Execution；
- InputVersion；
- StageRun；
- OperationPlan；
- ExecutionPlan；
- Domain State；
- 授权或版本事实源。

### 2.2 权威事实仍由确定性层提供

无论 Plan 是否启用：

- task / execution / scope / version 仍从 Authority Fixture / Resolver 获取；
- 写操作仍需预检；
- 缺范围仍必须澄清；
- stale conversation 不能覆盖 Authority；
- ToolResult / Domain State 才是业务事实来源。

## 3. 开始前先核实 AgentScope 2.0.8 Task Tools

必须在当前实际 `.venv` 中验证，而不是只看官方源码。

至少确认以下 import 或当前安装包真实可用的等价路径：

```python
from agentscope.tool._task import TaskCreate, TaskGet, TaskList, TaskUpdate
```

同时验证：

- Toolkit 能否注册；
- AgentState 是否能持有 task context；
- TaskCreate / TaskUpdate 是否能正常写入 AgentState；
- TaskList / TaskGet 是否能读回；
- 这些 Tool 是否需要 `_agent_state` 注入；
- 当前 qwen-plus 是否会自然调用这些 Task Tools。

如果当前 `.venv` 无法 import，必须记录为**环境事实**并停止 Task/Plan A/B，不得自己重实现一套“伪 AgentScope Plan”冒充原生能力。

## 4. A/B 条件

除 Plan 能力外，其余保持一致。

### A：plain_react

- 同一个 AgentScope Agent；
- 相同 Prompt；
- 相同业务 Toolkit；
- 相同 Authority Fixture；
- 不注册 AgentScope Task/Plan Tools。

### B：react_with_task_plan

- 同一个 AgentScope Agent；
- 相同 Prompt；
- 相同业务 Toolkit；
- 相同 Authority Fixture；
- 仅额外注册并允许 AgentScope 官方 Task/Plan Tools；
- 不允许通过 Prompt 强制每轮都创建 Task。

目的不是证明“有 Plan 看起来更完整”，而是比较：

> 原生 Task/Plan 自然存在时，是否让模型更稳定地完成多步骤任务。

不能通过写死“必须先 TaskCreate，再 TaskUpdate”来制造优势。

## 5. 必测多步骤场景

### P01｜切井后继续查询旧井层段

```text
Turn 1：查看 WELL-A 的 2035–2038m
Turn 2：切到 WELL-B 看报告
Turn 3：回到刚才那口井，再看看刚才那层
```

预期步骤：

```text
switch_session_well(WELL-A)
→
query_authority_result(刚才层段)
```

验证重点：

- 是否遗漏第二步；
- 是否查询错误 WELL-B；
- Authority 是否仍由 Tool/Resolver 绑定。

### P02｜修改 → 查询新版本 → 比较

使用 Mock 全井参数修改能力；不要引入真实层段修改。

用户：

```text
把全井孔隙度改成 0.16，
然后查一下修改后的结果，
再跟修改前比较一下
```

预期：

```text
preflight
→ apply
→ query new execution
→ compare old vs new
```

注意：

- 如果现有 012B Toolkit 没有 Apply/Compare，可复用 012A 的 Mock Tool；
- 仍然不能导入生产代码；
- old/new execution ID 必须来自 ToolResult / Authority，不允许模型编造。

### P03｜只查询后再比较

```text
查一下当前版本，
再跟上一版比较
```

预期：

```text
query CURRENT
→ query / resolve PREVIOUS
→ compare
```

如果 compare Tool 自身能直接解析两个 selector，可按 Tool Contract 执行，但必须记录实际步骤。

### P04｜中途发现需要澄清

```text
把孔隙度改成 0.16，
然后比较修改前后
```

如果修改范围缺失，预期：

```text
preflight
→ NEED_CLARIFICATION
→ stop
```

绝不能因为 Plan 中还有 compare 子任务就继续执行后续写入/比较。

### P05｜第一步失败后停止后续依赖

构造：

```text
修改一个不支持的参数 Rw
→ 查询新版本
→ 比较
```

预期：

```text
preflight → UNSUPPORTED
```

后续 apply / query-new / compare 不得执行。

### P06｜Authority 在步骤之间变化

场景：

```text
开始多步骤任务
→ 第一步完成
→ 脚本模拟 Authority current execution 外部变化
→ Agent 继续第二步
```

预期：

- 继续步骤前重新读取/校验 Authority；
- 不使用 Plan 中缓存的旧 execution 作为写/比较事实；
- stale 结果按当前 Tool Contract 返回。

## 6. 关键指标

不得只输出总分。

至少独立统计：

1. **Plan Usage**：是否创建 Task、是否更新状态、是否真正使用 Task/Plan。
2. **Step Completion**：预期步骤完成数 / 总步骤数，是否漏掉后续步骤。
3. **Step Order**：是否按依赖顺序执行，是否提前 compare / query-new。
4. **Stop-on-Failure**：NEED_CLARIFICATION / UNSUPPORTED / REJECTED 后是否停止依赖步骤。
5. **Authority Binding**：每一步 task / execution / scope / version 是否来自 Authority / ToolResult。
6. **Write Safety**：是否出现未预检写入、错误范围、stale 写入、非法 Apply。
7. **Grounding**：最终回复是否只陈述已实际执行的步骤和 ToolResult。
8. **Tool Route**：业务 Tool 是否正确。

Plan 使用率和业务正确率必须分开。

如果 B 组从未调用 Task/Plan Tool，则不能把任何差异归因于 Plan。

## 7. 重复次数

P01～P06：

- plain_react：每个场景至少 5 次；
- react_with_task_plan：每个场景至少 5 次；
- temperature=0；
- 相同输入、Prompt、业务 Tool 和 Fixture。

至少：

```text
6 cases × 5 repeats × 2 modes = 60 session runs
```

如一个场景包含多个 turn，逐 turn 记录。

## 8. Prompt 公平性

两组基础 Prompt 必须一致。

B 组允许 AgentScope 官方 Task Tools 自带的原生 Tool description / framework 注入；不能额外加入：

```text
你必须创建计划
你必须使用 TaskCreate
每一步完成后必须 TaskUpdate
```

否则实验只能证明“强制使用计划”，不能证明原生 Plan 自然有价值。

如果 qwen-plus 0 次调用 Task Tools，也要如实结论：

```text
REGISTERED_BUT_UNUSED
```

不要修改 Prompt 直到它被调用。

## 9. 实验代码与产物

在 `experiments/agentscope_native_poc/` 内最小扩展。

建议：

```text
plan_cases.json
plan_runner.py
```

如需复用 012A / 012B Tool，优先组合，不复制第二套业务事实模型。

产物：

```text
artifacts/task012c_<experiment_id>_plain.jsonl
artifacts/task012c_<experiment_id>_plan.jsonl
artifacts/task012c_summary.json
```

不得覆盖 012A / 012B 产物。

## 10. 离线合同测试

新增测试至少覆盖：

- 当前 `.venv` Task Tool import / 注册事实；
- 两组除 Task Tools 外 schema 一致；
- Plan Task 与业务 Authority Fixture 完全分离；
- Task Tool 不能修改 task_id / execution_id / scope / version；
- NEED_CLARIFICATION 后不得继续 Apply；
- UNSUPPORTED 后不得继续依赖步骤；
- Authority revision 改变后必须以新 Authority 为准；
- evaluator 将漏步骤、乱序、非法继续分别判错。

执行：

```bash
pytest tests/experiments -q
ruff check experiments tests/experiments
git diff --check
```

## 11. 判定规则

最终对 AgentScope Task/Plan 给出：

- `KEEP`
- `WRAP`
- `NO_CLEAR_VALUE`
- `REGISTERED_BUT_UNUSED`
- `VERIFY`

### 可考虑 WRAP / KEEP 的最低证据

不能只因为某一个 case 提升就采用。

至少需要同时满足：

- Task/Plan Tools 有真实调用；
- Step Completion 有稳定提升；
- 不降低 Write Safety；
- 不降低 Authority Binding；
- Stop-on-Failure 不变差；
- Grounding 不变差；
- 没有引入第二套业务事实源。

否则优先结论：`NO_CLEAR_VALUE / VERIFY`。

## 12. 设计文档回写

完成后必须检查并按证据更新：

- `docs/design/04-测井解释智能体技术方案.md`
- `docs/design/05-测井解释智能体测试与验收方案.md`
- 新增：`docs/evidence/07-Task012C-TaskPlan多步骤对照实验结果.md`
- 更新：`docs/tasks/012-agentscope-native-capability-poc.md`
- 更新：`docs/README.md`
- 更新：`docs/evidence/README.md`

01～03 只有在需求/目标/效果变化时才修改。

默认不修改 Current Implementation，因为本 Task 不改生产代码。

## 13. 完成报告

必须报告：

1. 当前 `.venv` AgentScope Task Tool import/注册事实；
2. 实验 ID；
3. 两组各多少 runs；
4. Task/Plan Tool 实际调用率；
5. P01～P06 每场景结果；
6. Step Completion / Order / Stop-on-Failure；
7. Authority Binding；
8. Write Safety；
9. Grounding；
10. Tool Route；
11. 最终 KEEP / WRAP / NO_CLEAR_VALUE / REGISTERED_BUT_UNUSED / VERIFY；
12. Architecture Issue；
13. Documentation Impact；
14. branch / commit / push；
15. tests / Ruff / diff-check。

## 14. 完成后停止

完成 Task 012C 后停止。

不要自动开始：

- HITL / Interrupt；
- Tracing；
- 生产 Plan 迁移；
- Prompt 大改。

先评审 Task/Plan 是否真的有价值。