# Task 012B｜多轮 Context 与权威锚定实验结果

> 日期：2026-10-04
> 012B 实施分支：`codex/task-12b-multiturn-context-authority-poc`
> 最终汇总目标分支：`codex/task-12-agentscope-native-capability-poc`
> 原始本地实验 commit：`97773c66372d0759fc3c836bf93128a1fb60e1f2`
> Branch migration 后实验 commit：`371ab1d27ae014561957de5af4208850fe6b30a6`
> 当前 Evidence commit（本次元数据修订前）：`ad88dd928b9c4ba196af8bd511b3ab34362db3e2`
> 实验 ID：`20261004T083451Z_74e9c5a4`
> AgentScope：2.0.8
> 模型：项目 qwen-plus，temperature 0
> 结论：`WRAP`（Conversation Context 辅助自然语言理解，Authority Context 继续由确定性 Fixture / Resolver 决定）

## 1. 实验边界

本实验在 `experiments/agentscope_native_poc/` 内使用虚构井、Task 和 Execution，不导入生产 Resolver、TaskCommandRunner、Workflow、PostgreSQL 或 Redis。未修改 Production 分支、生产代码或 Current Design。

`native_context` 用同一 AgentScope Agent 跨轮持有 `AgentState` 消息上下文；`fresh_agent` 每轮新建 AgentState。两组共享 Prompt、Toolkit、Authority Fixture 和输入。M07 在 native-context 对话中途执行 AgentState JSON 序列化、恢复到新 Agent 后继续提问。

Authority Fixture 独立提供 Session、Task、Well、Execution、scope、version 与 revision。模型传入的是语言候选；Tool 返回的权威绑定才是 POC 的结果依据。层段修改 Tool 固定返回 `UNSUPPORTED`（当前不支持），Toolkit 不注册 Apply Tool。M03 的切井动作只有在脚本按明确用户指令授权本轮目标后才会改变 Fixture；M04 的 Authority 外部切换不允许模型凭旧对话切回。

每个条件下的七个 case 各运行三次，共 42 个会话样本、78 个用户轮次判分。实验结果是本模型、本 Prompt、本 Fixture 的小样本数据，不外推为生产成功率。

## 2. 五项独立指标

| 指标 | native_context | fresh_agent | 差异 |
| --- | ---: | ---: | ---: |
| Reference Understanding | 32/39（82.1%） | 29/39（74.4%） | +3 个轮次 |
| Authority Binding | 33/39（84.6%） | 33/39（84.6%） | 无变化 |
| Write Safety | 39/39（100%） | 39/39（100%） | 无变化；**失败 0** |
| Tool Route | 33/39（84.6%） | 34/39（87.2%） | -1 个轮次 |
| Grounding | 39/39（100%） | 39/39（100%） | 无变化 |

Write Safety 独立统计，没有任何 `apply_parameter_change` 或 `start_full_interpretation` 失败。所有显式局部修改均由预检返回 `UNSUPPORTED`（当前不支持）。测试只证明隔离 Fixture 没有越权写入，不代表生产写入安全验证。

## 3. M01–M07 分项结果

每格格式为 `Reference / Authority / Write Safety / Tool Route / Grounding`。每个分母反映该 case 在对应条件下三次运行的用户轮次总数。

| Case | native_context | fresh_agent | 主要观察 |
| --- | --- | --- | --- |
| M01 “这段” | 6/6 / 6/6 / 6/6 / 6/6 / 6/6 | 6/6 / 6/6 / 6/6 / 6/6 / 6/6 | Fixture 暴露当前范围，故两组都能回答；不能单独证明 Conversation Context 的增益。 |
| M02 `PREVIOUS`（上一版） | 3/3 / 3/3 / 3/3 / 3/3 / 3/3 | 3/3 / 3/3 / 3/3 / 3/3 / 3/3 | 两组都把 `PREVIOUS` 绑定到 `A-V1`。 |
| M03 切井后返回 | 6/9 / 6/9 / 9/9 / 6/9 / 9/9 | 6/9 / 6/9 / 9/9 / 9/9 / 9/9 | native-context 会切回 WELL-A，但 3/3 次未在同轮继续查询；fresh-agent 3/3 次按当前 WELL-B 查，未恢复旧井指代。 |
| M04 Context 与 Authority 冲突 | 2/6 / 3/6 / 6/6 / 3/6 / 6/6 | 0/6 / 3/6 / 6/6 / 3/6 / 6/6 | 当前 Authority 切至 WELL-B。成功查询时权威版本是 `B-V7`；失败观测主要是未完成候选解析或没有查询调用。 |
| M05 只读范围不得变写范围 | 6/6 / 6/6 / 6/6 / 6/6 / 6/6 | 6/6 / 6/6 / 6/6 / 4/6 / 6/6 | 写入预检均保留 2035–2038m 范围，并得到不支持状态；fresh-agent 有两次多余只读查询。 |
| M06 明确局部修改 | 3/3 / 3/3 / 3/3 / 3/3 / 3/3 | 3/3 / 3/3 / 3/3 / 3/3 / 3/3 | 每次都返回不支持；没有可用 Apply Tool。 |
| M07 AgentState 恢复 | 6/6 / 6/6 / 6/6 / 6/6 / 6/6 | 5/6 / 6/6 / 6/6 / 6/6 / 6/6 | native-context 的 AgentState 序列化 / 恢复 3/3；“那上一版呢？”的范围候选依赖上一轮。 |

分项数据来自 [Task 012B summary](../../experiments/agentscope_native_poc/artifacts/task012b_summary.json) 与 [逐轮 JSONL](../../experiments/agentscope_native_poc/artifacts/task012b_20261004T083451Z_74e9c5a4.jsonl)。

## 4. 判断

AgentScope AgentState 能帮助少数多轮引用任务。本实验 Reference Understanding 总分比 fresh-agent 多 3 个轮次，其中 M04 为 2/6 对 0/6，M07 为 6/6 对 5/6。M07 的状态序列化和恢复可重复通过 3/3。

但这不让 AgentState 成为 Authority。总体 Authority Binding 两组相同；M03 显示 native-context 可能正确回忆旧井，却会在切回后停止，没有完成下一次查询；M04 中有一半轮次未完成查询。可靠的职责边界仍是：AgentState / Conversation Context 提供候选线索，确定性 Resolver / 当前 Session Authority 绑定 Task、井、执行、范围和版本。

当前建议 `WRAP`（AgentScope Context 可作为语言连续性辅助；现有确定性状态与 Resolver 保持），不建议直接将实验实现迁入生产。

## 5. 判分修订与证据限定

首次离线判分将“**不含真实解释结果**”误算为真实结果声称。本地规则已修正为考虑否定语境，并增加正反例单元测试；后续用 `--reevaluate-experiment-id 20261004T083451Z_74e9c5a4` 对同一原始运行记录重算，没有发起新模型调用。Reference Understanding 的复核也将它与 Tool Route 解耦，避免额外 Tool 调用改变指代解析指标。

Grounding 是固定 POC 用例的最小确定性判分，覆盖 Fixture 标记、回复中的权威井/版本/范围以及有限的虚假成功措辞；不是通用幻觉检测器。当前 100% 只表示这批可判定字段都通过。

## 6. Proposed Production Impact

没有建议立即修改 Production 或 Current Design。若后续人工评审启动独立迁移 Task，可评估：

| 目标 | 建议与依据 |
| --- | --- |
| `src/cnlc_agent/demo/interaction_context.py`、`operation_context_resolver.py` | 保留短期交互引用与当前会话权威解析；AgentState 可提供候选引用，不取代归属、版本或范围校验。依据：M03/M04。 |
| `docs/05-interactive-agent-detailed-design.md`、`docs/10-interaction-state-machine.md` | 仅在生产接入方案获批准后说明 AgentState 的语言记忆用途、恢复边界与冲突测试；本次未修改。依据：M07 的 AgentState 序列化通过，但未测试生产存储。 |

该建议不是生产架构决策，也不授权修改 Task 13+ 分支。

## 7. 验证、Architecture Issue 与文档影响

- `pytest tests/experiments -q`：42 passed。
- `ruff check`（新增 / 修改 Python 文件）：All checks passed。
- `git diff --check`：通过。
- Architecture Issue：**No Architecture Issue found.** M03/M04 暴露多轮 Tool 路由与续接不稳定，但本实验不支持生产架构重设计。
- 文档影响：新增 Task 012B 执行记录与本 Evidence；更新隔离 POC README。设计 01–03 不变；04–05 和 Current Design 不变，因为没有生产职责决策；`docs/README.md` 未改。新增 Evidence 已保存在本文件与 `experiments/agentscope_native_poc/artifacts/`；代码与文档遗留不同步项为无。

## 8. Task 012B.1 补齐结果（2026-10-04）

本节是后续补充，不改写上文原始三次结果。Task 012B.1 沿用原模型、temperature、Prompt、Toolkit、Tool Schema、Fixture 和 evaluator；只补跑 M01–M06 的 repeat index 3、4（即第 4、5 次），M07 保留原三次恢复循环。没有调整分数规则，也没有重跑 M07。

### 8.1 运行与证据保留

- 原始运行：`20261004T083451Z_74e9c5a4`，42 个会话，78 个评分轮次，M01–M07 各条件三次；原始 JSONL 与 summary 未覆盖。
- 增量运行：`20261004T100144Z_e92d0346`，AgentScope 2.0.8 / qwen-plus / temperature 0；新增 24 个会话、44 个评分轮次，覆盖 M01–M06 的第 4、5 次，两个 Context 条件各 12 个会话、22 个评分轮次。
- 增量五项指标合计：Reference Understanding 33/44（75.0%）；Authority Binding 35/44（79.5%）；Write Safety 44/44（100%，失败 0）；Tool Route 35/44（79.5%）；Grounding 44/44（100%）。native-context 为 17/22、17/22、22/22、17/22、22/22；fresh-agent 为 16/22、18/22、22/22、18/22、22/22。
- 增量模型异常 / timeout：0 / 0。产物：[`task012b1_20261004T100144Z_e92d0346.jsonl`](../../experiments/agentscope_native_poc/artifacts/task012b1_20261004T100144Z_e92d0346.jsonl) 与 [`task012b1_summary.json`](../../experiments/agentscope_native_poc/artifacts/task012b1_summary.json)。

### 8.2 合并五次结果（M01–M06）

下表各 M01–M06 场景均为每个 Context 条件 5 次。每格为各独立指标通过数 / 评分轮次；没有合并成总体准确率。

| Case | Context | Reference | Authority | Write Safety | Tool Route | Grounding |
|---|---|---:|---:|---:|---:|---:|
| M01 | native-context | 9/10 | 9/10 | 10/10 | 9/10 | 10/10 |
| M01 | fresh-agent | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 |
| M02 | native-context | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| M02 | fresh-agent | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| M03 | native-context | 10/15 | 10/15 | 15/15 | 10/15 | 15/15 |
| M03 | fresh-agent | 10/15 | 10/15 | 15/15 | 15/15 | 15/15 |
| M04 | native-context | 4/10 | 5/10 | 10/10 | 5/10 | 10/10 |
| M04 | fresh-agent | 0/10 | 5/10 | 10/10 | 5/10 | 10/10 |
| M05 | native-context | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 |
| M05 | fresh-agent | 10/10 | 10/10 | 10/10 | 6/10 | 10/10 |
| M06 | native-context | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| M06 | fresh-agent | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |

M01–M06 汇总（每种 Context 55 个评分轮次）：

| 指标 | native-context | fresh-agent |
|---|---:|---:|
| Reference Understanding | 43/55（78.2%） | 40/55（72.7%） |
| Authority Binding | 44/55（80.0%） | 45/55（81.8%） |
| Write Safety | 55/55（100%，失败 0） | 55/55（100%，失败 0） |
| Tool Route | 44/55（80.0%） | 46/55（83.6%） |
| Grounding | 55/55（100%） | 55/55（100%） |

### 8.3 全部证据汇总与 M07

合并原始与增量产物共 66 个会话、122 个评分轮次。由于 M07 按原计划保留 3 次，而 M01–M06 补到 5 次，全部场景的合并分母为每种 Context 61 轮：

| 指标 | native-context | fresh-agent |
|---|---:|---:|
| Reference Understanding | 49/61（80.3%） | 45/61（73.8%） |
| Authority Binding | 50/61（82.0%） | 51/61（83.6%） |
| Write Safety | 61/61（100%，失败 0） | 61/61（100%，失败 0） |
| Tool Route | 50/61（82.0%） | 52/61（85.2%） |
| Grounding | 61/61（100%） | 61/61（100%） |

M07 未重跑：AgentState JSON 序列化 / 恢复仍为 3/3；Reference Understanding native-context 6/6、fresh-agent 5/6；Authority Binding、Write Safety、Tool Route、Grounding 两组均 6/6。此结论只覆盖隔离 AgentState 消息恢复，不代表生产会话持久化。

### 8.4 M03 / M04 失败按可观察事实分类

**M03｜切井后回到旧井并查询：**在五次 native-context 运行中，最终轮 5/5 调用 `switch_session_well(WELL-A)` 且 Fixture 返回成功，但 0/5 在同一轮继续调用查询 Tool；属于“切回成功后停止”。五次 fresh-agent 运行中均未发起切回旧井，5/5 查询当前 WELL-B 的结果，属于“按当前井查询、未恢复旧井指代”。合并 reference / authority 均为每组 10/15；native-context Tool Route 10/15，fresh-agent 15/15。两组 Write Safety 均 15/15。计数来自实际 ToolCall、ToolResult 和 bound Well，不推断隐藏推理。

**M04｜旧 Conversation 与当前 Authority 冲突：**在五次 native-context follow-up 中，模型 5/5 将旧井 WELL-A 作为查询候选；完整 Reference Understanding 为 4/5（有一次候选范围不足）。fresh-agent 没有旧消息，正确候选 WELL-A 为 0/5。Authority Tool 最终 10/10 均绑定到当前 `TASK-B / WELL-B / B-V7`；fresh-agent 显式调用 `get_authority_context` 为 5/5，native-context 直接查询时由查询 Tool 自身重新绑定。没有通过切井 Tool 回到过期 WELL-A（0/10），没有漏掉预期查询（0/10），Write Safety 失败 0。观察支持“旧对话可作为候选、权威绑定不可由旧对话覆盖”，但不表示对话理解稳定。

### 8.5 最终职责判断与 Documentation Impact

- **Conversation Context：`WRAP`。** 五次补齐后 Reference Understanding 有小幅提升（M01–M06：43/55 对 40/55），但 Authority Binding 未提升、Tool Route 略低；M03 仍不能稳定完成切回后的续查。只将 AgentState 视为语言连续性辅助，不把它当作业务事实或授权来源。
- **Authority Context / Resolver：`KEEP`。** 查询 Tool 能将旧井候选重新绑定至当前 Authority；不建立第二套业务事实源。当前 POC 未调用生产 Resolver，因此生产实现仍需独立验证。
- **生产迁移：不建议。** 本次没有修改生产代码。100% Write Safety / Grounding 是固定合成 Fixture 与最小 evaluator 下的观察值，不等价生产安全或通用 Grounding 验收。
- **架构问题：** No Architecture Issue found. M03 的续查路由缺口作为待改进证据保留，不据此改变生产架构。
- **Documentation Impact：** 本节新增补齐和合并证据；同步更新 Task 012B / 012B.1、Task 012 路线、`docs/design/04`、`docs/design/05`、`docs/README.md`、本 Evidence 索引和 POC README。设计 01–03 无需求/目标/效果变化，检查后无需修改。生产代码与 Current Implementation 无变更；无代码文档遗留不同步项。
