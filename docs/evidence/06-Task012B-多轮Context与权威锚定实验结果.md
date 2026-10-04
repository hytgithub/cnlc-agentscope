# Task 012B｜多轮 Context 与权威锚定实验结果

> 日期：2026-10-04
> POC 分支：`codex/task-12-agentscope-native-capability-poc`
> Commit A：`97773c6`
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
