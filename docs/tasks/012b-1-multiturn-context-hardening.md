# Task 012B.1｜多轮 Context 实验补齐与设计回写

> 父任务：Task 012 / 012B
> 分支：`codex/task-12-agentscope-native-capability-poc`
> 类型：实验补齐 / 证据闭环 / 文档回写
> 日期：2026-10-04
> 推荐执行模型：GPT-6 Luna
> 原则：不修改生产代码；不通过调 Prompt / Tool Contract 追求更高分；保留 Task 012B 原始证据。

## 1. 背景

Task 012B 已完成真实 qwen-plus 多轮 Context POC，并形成：

- `docs/evidence/06-Task012B-多轮Context与权威锚定实验结果.md`
- `experiments/agentscope_native_poc/artifacts/task012b_20261004T083451Z_74e9c5a4.jsonl`
- `experiments/agentscope_native_poc/artifacts/task012b_summary.json`

现有结果已经提供正向证据：

- Conversation Context 的 Reference Understanding：82.1%，高于 fresh-agent 的 74.4%；
- Authority Binding 两组均 84.6%；
- Write Safety 两组均 100%，本轮写安全失败 0；
- AgentState 序列化 / 恢复 3/3；
- 当前阶段性架构判断为：Conversation Context = `WRAP`；Authority Context / Resolver 继续承担权威绑定。

但原 Task 012B 明确要求：

> M01～M06 每个场景真实 qwen-plus 至少重复 5 次；M07 至少 3 次。

实际 M01～M06 仅执行 3 次。因此 Task 012B 尚未完全满足自己的退出条件；同时 Evidence 06 尚未正式回写 `docs/design/04` 与 `docs/design/05`。

本 Task 只负责补齐这一缺口。

## 2. 硬边界

本 Task 不得：

- 修改 `src/cnlc_agent/**`；
- 修改生产 Resolver / InteractionContext / Validator / Policy / StageOrchestrator；
- 接 PostgreSQL、Redis、真实公司 API；
- 修改 W01～W10；
- 修改 M01～M07 的业务语义、期待结果或安全口径；
- 为提升分数而调整 Prompt、Tool Description、Tool Schema 或 Authority Fixture；
- 引入 Skill、Task/Plan、HITL、Tracing 等新变量；
- 覆盖 Task 012B 原始 JSONL / summary；
- 把 POC 结果描述成生产成功率。

如发现 runner / evaluator 的确定性 bug，可以修复，但必须：

1. 单独记录 bug；
2. 保留修复前证据；
3. 说明是否影响原 3 次判分；
4. 不把不同判分规则的数据静默混为一个统计口径。

## 3. 追加运行要求

### 3.1 M01～M06

在以下两个条件下，各追加第 4、5 次：

- `native_context`
- `fresh_agent`

也就是至少新增：

```text
6 cases
× 2 added repeats
× 2 context modes
= 24 additional session samples
```

追加运行必须使用与原 Task 012B 相同的：

- qwen-plus；
- temperature=0；
- Prompt；
- Toolkit / Tool Schema；
- Authority Fixture；
- cases 输入；
- evaluator 规则（除非发现并明确修复确定性 bug）。

### 3.2 M07

原 Task 已完成 3 次 AgentState 恢复循环，满足最低要求。

默认不要为了凑样本额外重跑 M07；如确需重跑，必须单独标明为附加证据，不改变原验收分母定义。

## 4. 证据保留

不得覆盖：

```text
task012b_20261004T083451Z_74e9c5a4.jsonl
task012b_summary.json
```

新增唯一命名文件，例如：

```text
task012b1_<experiment_id>.jsonl
task012b1_summary.json
```

最终 Evidence 06 必须同时列出：

1. 原始 3 次实验 ID；
2. 新增第 4、5 次实验 ID；
3. 原始统计；
4. 增量统计；
5. 合并后的 5 次统计；
6. M07 保持 3 次的说明。

不得只留下合并结果而丢失原始证据。

## 5. 必须重点分析 M03 / M04

不要为了让 M03 / M04 通过而调 Prompt。

需要对新增运行中的失败按可观察事实分类，例如：

### M03｜切井后返回

分别统计：

- 正确理解“刚才那口井”；
- 是否成功切回目标井；
- 切回后是否继续执行预期查询；
- 是否出现“切回成功但停止，没有继续查询”；
- 是否错误读取当前 WELL-B。

### M04｜Conversation 与 Authority 冲突

分别统计：

- 是否理解 conversation 中旧引用；
- 是否重新读取 Authority；
- 是否最终绑定当前权威 WELL / Task / Execution；
- 是否因为旧 conversation 尝试切回过期对象；
- 是否没有完成预期查询；
- 是否发生任何写安全失败。

失败原因必须来自 ToolCall / ToolResult / Authority State / 最终回复等可观察证据，不推测模型隐藏推理。

## 6. 五项指标继续独立统计

保持 Task 012B 的五项指标：

1. `Reference Understanding`
2. `Authority Binding`
3. `Write Safety`
4. `Tool Route`
5. `Grounding`

不得输出一个平均“总准确率”代替这些指标。

特别要求：

> Write Safety 失败必须单独列出，不能被其他成功样本平均掉。

## 7. 012B 最终判定规则

补齐后给出明确结论：

### Conversation Context

从以下选择并说明证据：

- `KEEP`
- `WRAP`
- `NO_CLEAR_VALUE`
- `VERIFY`

当前已有证据倾向 `WRAP`，但最终必须以补齐后的 5 次结果为准。

### Authority Context / Resolver

判断：

- 是否继续 `KEEP`；
- Conversation 是否产生第二套业务事实源风险；
- stale conversation 与 Authority 冲突时是否始终以 Authority 为准。

### 生产迁移

本 Task 默认结论仍应是：

> 不直接迁移 POC 到生产。

只有证据足以提出生产迁移建议时，也只能写成后续独立 Task，不得在本 Task 修改生产代码。

## 8. 回写主设计文档

这是本 Task 的强制完成条件。

### 8.1 更新 `docs/design/04-测井解释智能体技术方案.md`

至少检查并更新：

- 顶部状态中“多轮 Context 待验证”的表述；
- AgentScope 优先配置与验证矩阵中的“连续交互”；
- 当前技术决策表中的 Memory / State；
- 明确：
  - Conversation Context 只提供语言候选；
  - Authority Context / Resolver 才绑定 task / execution / scope / version；
  - Conversation memory 不能覆盖 current / active / view 等权威状态；
  - AgentState 恢复能力的证据边界仅限隔离 POC。

如果最终仍证据不足，必须写 `PARTIAL / VERIFY`，不要为了完成 Task 写成已验证。

### 8.2 更新 `docs/design/05-测井解释智能体测试与验收方案.md`

至少检查并更新：

- 顶部“多轮 Context 仍待执行”的旧表述；
- 第一轮已执行证据；
- A05 连续上下文状态；
- 配置优先决策表中的连续上下文；
- 写入 012B / 012B.1 的真实样本量和五项指标；
- 明确 POC 的 100% Write Safety / Grounding 不等于生产安全/通用 Grounding 已验收。

### 8.3 更新 Evidence 06

保留原结论历史，新增“012B.1 补齐结果”章节，不重写得像原 3 次从未发生。

至少包含：

- 原始 3 次；
- 新增 2 次；
- 合并 5 次；
- M01～M07 结果；
- M03/M04 失败分类；
- 最终职责判断；
- Documentation Impact。

## 9. 文档一致性

同步检查：

- `docs/tasks/012b-multiturn-context-authority-poc.md`
- `docs/tasks/012-agentscope-native-capability-poc.md`
- `docs/README.md`
- `docs/evidence/README.md`

Task 012 路线应把：

```text
012B = 已执行但原重复数不足
012B.1 = 补齐中 / 完成后关闭012B
```

正确反映出来。

## 10. 测试

至少执行：

```bash
pytest tests/experiments -q
ruff check experiments tests/experiments
git diff --check
```

如果修改 evaluator / runner，必须新增或更新对应离线单测。

真实模型运行必须显式记录：

- model；
- temperature；
- experiment_id；
-新增 sample 数；
-是否有模型异常 / timeout；
-原始 artifact 路径。

## 11. 完成报告

最终报告必须包含：

1. 原始 012B 实验 ID；
2. 012B.1 新实验 ID；
3. 新增样本数；
4. 合并后的五项指标；
5. M03 / M04 失败分类；
6. Conversation Context 最终判断；
7. Authority Context / Resolver 最终判断；
8. 04 / 05 实际更新内容；
9. tests / Ruff / diff-check 结果；
10. Architecture Issue；
11. Documentation Impact；
12. branch / commit / push 状态。

只要 M01～M06 没达到总计 5 次，或者 04/05 没同步回写，Task 012B.1 不得标记完成。

## 12. 完成后停止

完成 012B.1 后停止。

不要自动开始：

- Task/Plan A/B；
- HITL / Interrupt；
- Tracing；
- 生产 Context 迁移。

先评审最终 Context 证据，再决定下一项。