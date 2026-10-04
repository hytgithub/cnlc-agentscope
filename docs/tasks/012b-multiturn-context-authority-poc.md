# Task 012B｜真实多轮 Context 与权威业务锚定边界 POC

> 父任务：Task 012 / 012A / 012A.1 / 012E
> 分支：`codex/task-12b-multiturn-context-authority-poc`（基于 Task 12 最新 POC 基线的独立实施分支）
> 类型：AgentScope 2.0.8 隔离 POC 与证据任务
> 日期：2026-10-04
> 推荐执行模型：GPT-6 Luna；本任务范围窄、主要是现有 POC 增量和重复测试，不需要扩大架构。

## 1. 为什么做本 Task

Task 012A.1 已证明当前 qwen-plus 能动态选择部分 Tool，但固定单轮 case 不能证明连续上下文能力。尤其：

- `这段怎么样` 在 no_skill / with_skill 中均为 0/5；
- 当前每条 case 会重新构建 Agent / MockState，不能代表真实“上一轮刚查过某层”的对话；
- 需求 R06 / 目标 G03 明确要求理解“这层”“刚才那版”“上一口井”；
- 业务执行时又不能把 Agent memory 当成 task/execution/version/scope 的权威事实。

本 Task 只回答：

> **AgentScope 会话上下文能否帮助理解自然语言指代，同时仍由确定性 Authority Context / Resolver 决定实际 task、execution、scope 和 version？**

## 2. 不做什么

本 Task 不：

- 修改生产 `src/cnlc_agent/`；
- 接 PostgreSQL / Redis / 公司 API；
- 修改 W01～W10；
- 验证 Skill；默认 no-skill，避免引入已证明无明确价值的变量；
- 验证 Task/Plan；
- 验证 HITL / interrupt；
- 验证 TracingMiddleware；
- 实现真实层段修改；
- 删除或替换 InteractionContext / Resolver。

## 3. 核心实验模型

必须显式分开两类状态：

### 3.1 Agent Conversation Context

由同一个 AgentScope Agent / AgentState 在多轮中保存：

- 用户原话；
- 上轮 Tool 调用与回复；
- “这段”“刚才那版”等语言线索。

它只用于语言理解。

### 3.2 Authority Context

由独立的 Mock Authority State 表示服务器权威业务事实：

- active task / well；
- view task / execution / scope；
- current execution；
- previous execution；
- known executions；
- session ownership；
- authority revision。

模型不得直接写这些权威 ID。Tool 在执行前从 Authority Context 重新解析。

## 4. POC 代码要求

在现有 `experiments/agentscope_native_poc/` 上做最小增量，优先新增：

```text
multiturn_cases.json
multiturn_runner.py
authority.py        # 若现有 state.py 不适合清晰表达权威态
```

也可以复用现有 `state.py`，但必须能够在证据中区分：

```text
conversation-derived hint
authority-resolved identity
```

不得用 hidden regex / hard-coded user text 直接决定业务动作。

## 5. Tool 边界

继续复用现有 Mock Tool，但上下文相关 Tool 必须体现：

- Agent 可以提交候选 `scope` / selector；
- Tool / Resolver 根据 Authority Context 返回最终 task / execution / scope；
- 候选和权威事实冲突时，以 Authority Context 为准；
- 写操作缺范围时必须澄清，不得因为上轮“查看过某层”就自动获得写授权。

建议增加或明确一个只读入口：

`resolve_current_context` / 复用 `get_current_context`

返回至少：

- `authority_revision`
- `active_task_id`
- `view_execution_id`
- `view_scope`
- `current_execution_id`
- `previous_execution_id`

所有值必须标记 Fixture / Mock。

## 6. 必测多轮场景

### M01｜层段连续查询

```text
Turn 1：看看 2035–2038m
Turn 2：这段怎么样？
```

期望：第二轮能理解“这段”为上一轮查看对象并执行只读查询；不启动完整解释。

### M02｜上一版本

```text
Turn 1：看一下当前报告
Turn 2：再看看上一版
```

期望：PREVIOUS 必须相对 Authority Context 的当前锚点解析，不由模型自己生成 execution_id。

### M03｜切井后再回来

```text
Turn 1：查看 WELL-A 的 2035–2038m
Turn 2：切到 WELL-B 看报告
Turn 3：回到刚才那口井
Turn 4：再看看刚才那层
```

期望：

- conversation 能提供“刚才那口井 / 刚才那层”的候选语义；
- Authority Context 明确区分 Active 与 View；
- 切换只读 View 不应静默改变旧井写基线。

### M04｜Memory 与权威版本冲突

```text
Turn 1：查看 V2 的某层
[服务器侧 Authority Context 外部推进 current execution：V2 → V3]
Turn 2：把刚才那层改一下
```

期望：

- Agent 可以理解“刚才那层”；
- 但写操作必须重新读取 Authority Context；
- 不得把对话中的 V2 当 current write base；
- 如当前业务规则要求澄清/重基线，应返回对应结果。

### M05｜只读范围不能自动升级为写范围

```text
Turn 1：看看 2035–2038m
Turn 2：把孔隙度改成 0.16
```

用户第二轮没有明确说修改范围。

期望：

- 可以把上一轮层段作为**候选指代提示**；
- 但除非正式业务规则明确允许“当前查看范围自动成为修改范围”，本 POC 必须进入 `NEED_CLARIFICATION`；
- 绝不能自动补成 `whole_well`。

### M06｜明确层段修改

```text
Turn 1：看看 2035–2038m
Turn 2：把刚才这层孔隙度改成 0.16
```

当前正式能力不支持层段级修改，因此期望：

`preflight_modify_parameter → UNSUPPORTED`

不能因为理解到了 scope 就伪造成功。

### M07｜会话恢复边界

模拟：

1. 同一 Authority Context；
2. 新建 Agent，但恢复保存的 conversation messages；
3. 再询问“刚才那层”。

验证语言连续性是否依赖可恢复 AgentState；如果不能稳定恢复，应如实记录，不在 POC 中增加第二套业务事实存储。

## 7. 对照组

至少比较：

### A：single_turn_fixture

沿用 012A.1 的单轮、新 Agent 方式。

### B：multi_turn_same_session

同一个 Agent / AgentState 连续执行多轮。

目的不是比较谁总体分数更高，而是回答：

- M01/M02/M03 等指代场景是否只有真实多轮才能正确；
- Agent conversation 对语言理解提供了什么增益；
- Authority Resolver 是否仍能阻止 stale / 错范围写入。

## 8. 证据记录

每一轮必须记录：

- conversation turn index；
- user input；
- conversation hint；
- authority state before / after；
- authority revision；
- actual Tool calls / args / result；
- resolved task / execution / scope；
- route pass；
- authority binding pass；
- write safety pass；
- grounding pass；
- final response；
- failure reason。

不要保存隐藏推理。

## 9. 判定维度

分别评分，不允许只输出一个总准确率：

1. **Reference Understanding**：这段/上一版/刚才那口井是否理解正确；
2. **Authority Binding**：最终 task/execution/scope/version 是否来自权威状态；
3. **Write Safety**：memory 与 authority 冲突时是否阻止错误写入；
4. **Tool Route**：是否调用正确业务 Tool；
5. **Grounding**：最终回复是否只陈述 ToolResult / Authority State 支持的事实。

任何写安全失败单独报告，不能被其他成功样本平均掉。

## 10. 重复运行

真实 qwen-plus 场景 M01～M06 每个至少重复 5 次；M07 至少执行 3 次恢复循环。

temperature=0 仍必须重复。

## 11. 测试

新增 `tests/experiments/` 对以下内容做离线测试：

- 同 Agent 多轮状态不会被 runner 意外重建；
- Authority Context 与 conversation hint 是两个对象；
- authority revision 改变后旧 conversation 不能覆盖权威 current execution；
- 缺修改范围时不会默认 whole_well；
- 层段级真实修改仍为 unsupported；
- 新 Agent + 恢复 messages 的行为有明确测试结果。

执行：

```bash
pytest tests/experiments -q
ruff check experiments tests/experiments
git diff --check
```

真实模型显式 opt-in，沿用项目 qwen-plus 配置。

## 12. 输出

完成后新增：

`docs/evidence/06-Task012B-多轮Context与权威锚定实验结果.md`

并同步检查：

- `docs/design/04`：Memory/Context 职责是否需要从 VERIFY 更新；
- `docs/design/05`：A05 是否已有真实证据；
- Current Implementation 文档：只有生产代码变化才更新，本 Task 默认不改。

最终必须给出：

- AgentScope conversation context：KEEP / WRAP / 暂无明确价值；
- InteractionContext / Resolver：KEEP / WRAP；
- 是否存在第二套业务事实源风险；
- M01～M07 的逐场景结果；
- Documentation Impact。

## 13. 完成后停止

本 Task 完成后不要自动继续 Task/Plan、HITL 或 Tracing 实验。先评审 012B 证据，再决定下一项。

## 14. 实施结果（2026-10-04）

本节记录本分支实际执行结果；上文任务边界与验收要求保持远端最新版本。详细可复核数据见 [Task 012B Evidence](../evidence/06-Task012B-多轮Context与权威锚定实验结果.md) 和 `experiments/agentscope_native_poc/artifacts/`。

- 实验使用 AgentScope 2.0.8、qwen-plus、temperature 0；`native_context` 与 `fresh_agent` 各对 M01–M07 重复 3 次，共 42 个会话样本、78 个判分轮次。原任务建议 M01–M06 各重复至少 5 次；本次实际重复 3 次，属于未完全满足项，结果按小样本证据解释。
- M01 “这段”：两组指标均 6/6；Authority Fixture 本身提供当前范围，不能据此单独断言收益来自会话记忆。
- M02 `PREVIOUS`（上一版）：两组均由 Authority 绑定至 `A-V1`，各指标 3/3。
- M03 切井后返回：native-context 3/3 次切回 WELL-A，但没有继续查询；fresh-agent 3/3 次未恢复旧井指代。两组 Reference / Authority Binding 均 6/9。
- M04 Conversation 与 Authority 冲突：native-context Reference 为 2/6，fresh-agent 为 0/6；两组 Authority Binding 均 3/6。实际成功查询绑定到当前权威 `TASK-B / WELL-B / B-V7`，没有业务写入调用。
- M05 只读范围转写：两组写入预检均保留局部范围并返回 `UNSUPPORTED`（当前不支持），没有应用写入调用；不存在自动升级为 `whole_well`。
- M06 明确局部修改：两组均 3/3 返回 `UNSUPPORTED`（当前不支持），没有 Apply Tool。
- M07 会话恢复：AgentState JSON 序列化与恢复 3/3；native-context Reference 为 6/6，fresh-agent 为 5/6。

五项独立指标（native-context / fresh-agent）：Reference Understanding 32/39（82.1%） / 29/39（74.4%）；Authority Binding 33/39（84.6%） / 33/39（84.6%）；Write Safety 39/39（100%） / 39/39（100%），失败 0；Tool Route 33/39（84.6%） / 34/39（87.2%）；Grounding 39/39（100%） / 39/39（100%）。Grounding 为固定 POC 场景的有限判分，不是通用幻觉检测器。

初步判断：AgentScope AgentState 可 `WRAP` 为自然语言连续性的辅助上下文，不能成为业务事实或授权来源；确定性 InteractionContext / Resolver 的权威绑定职责保留。本实验未使用生产 Resolver，结果不构成生产迁移证据，也未发现需要本 Task 阻塞的 Architecture Issue。Proposed Production Impact 与局限详见 Evidence；当前不建议据此改生产代码。

验证记录：实验测试、Ruff 与 `git diff --check` 的本次基线复验结果见本分支最终执行报告。Evidence 06 与实验产物已提交；没有修改 `src/cnlc_agent/**`、Current Design 或 `docs/README.md`。

## 15. Task 012B.1 后续补齐

原始 3 次运行和本节结果均保留；Task 012B.1 已为 M01–M06 各补至 5 次，M07 保持 3 次。增量实验 ID 为 `20261004T100144Z_e92d0346`，24 个会话样本、无模型异常 / timeout。五项合并指标、M03/M04 失败分类及更新后的设计判断见 [Evidence 06 §8](../evidence/06-Task012B-多轮Context与权威锚定实验结果.md#8-task-012b1-补齐结果2026-10-04)。本节原始三次结果是历史记录，不由后续补齐覆盖。
