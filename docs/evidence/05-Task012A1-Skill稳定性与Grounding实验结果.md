# Task 012A.1｜Skill 稳定性与 Grounding 实验结果

> 分支：`codex/task-12-agentscope-native-capability-poc`
> 模型：项目 qwen-plus 配置
> 实验 ID：`20261004T034634Z_31edf5c1`
> 日期：2026-10-04
> 结论：`NO_CLEAR_VALUE`（当前 qwen-plus 配置下没有证明 Skill 正文对路由有明确价值）

## 1. 执行情况与历史记录

本轮真实模型共执行 120 个独立场景运行：no_skill 60 条、with_skill 60 条。固定集 16 个场景；case `02`、`03`、`05`、`06`、`07`、`09`、`10`、`13`、`14`、`15`、`16` 各重复 5 次，其余 5 个场景各运行一次。temperature 为 0；单条运行没有记录到模型异常。每组 59/60 条可取得最终消息 token usage。本轮真实调用发生在 runner 加入显式 `asyncio.wait_for` 超时之前，因此没有记录一个可归因于实验应用层的单样本超时值；本 Task 后续 runner 已新增可配置 120 秒默认超时和续跑能力，该配置没有追溯套用到本轮评分。

模型运行记录：

- `artifacts/native_poc/task012a1_20261004T034634Z_31edf5c1_no_skill.jsonl`
- `artifacts/native_poc/task012a1_20261004T034634Z_31edf5c1_with_skill.jsonl`
- 聚合和逐样本配对判分：`artifacts/native_poc/summary.json`

每个 A/B 样本的输入、基础 Prompt、8 个领域 Tool Schema 和初始 Fixture 均一致；120 个配对检查全部通过。模型调用先后顺序按重复序号交替。现有 Task 012A 原始 JSONL 没有覆盖。

开始 Task 012A.1 时，指定先读的 `docs/evidence/04-Task012A-Prompt-Toolkit-Skill实验结果.md` 在仓库中不存在；旧结果取自已有 `summary.json` 和 12+12 条原始 JSONL，且保留旧摘要供追溯。旧轮 no_skill 为 7/12、with_skill 为 9/12，SkillViewer 调用为 0。

## 2. AgentScope 2.0.8 实际实现核对

依据当前 `.venv` 安装源码：

- `Toolkit(skills_or_loaders=...)` 将 loader 注册到 ToolGroup；默认 Skill 指令把 Skill 的 name、description、dir 放入 Agent 有效 system prompt，并告诉模型匹配时要使用内置 Viewer 读取正文。
- `LocalSkillLoader` 读取 `SKILL.md` frontmatter 中的 `name` / `description`，并将 Markdown 正文保存在 `Skill.markdown`。这是本地加载，不代表模型已获得正文。
- 内置 Viewer 的真实 Tool 名称为 `Skill`，JSON Schema 要求 `{ "skill": "精确 Skill 名称" }`；ToolResult 返回所选 Skill 的完整 Markdown。
- `Agent._get_system_prompt()` 附加元信息；没有 `Skill` ToolCall 和对应正文 ToolResult 时，不能判定模型读过完整 Skill。

本轮用记录字段分别核对注册、元信息可见、Viewer 调用、正文返回。with_skill 中四个 Skill 名称和描述可见；但 60 个 with_skill 运行中 `Skill` 调用 0 次，20 个 Skill-required 运行中也是 0/20，正文返回模型 0 次。no_skill 运行亦没有 SkillViewer 调用。

AgentScope 本身的 Toolkit 注册、元信息注入、Viewer Tool 注册和离线 Viewer 读取能力均通过离线测试；但本轮 qwen-plus 没有在真实对话中主动调用 Viewer。现有观测无法揭示模型内部未调用原因。可能解释包括模型认为 Tool Schema 足够、触发描述不够显著或 qwen-plus 当前选择策略；这些都是推断，不是内部推理事实。未保存或收集模型隐藏推理。

## 3. 聚合指标

比率分母按运行数或对应目标用例数计算；Grounding 失败会使 overall 失败，即使 route 通过。

| 指标 | no_skill | with_skill |
| --- | ---: | ---: |
| Tool route 成功 | 39/60（65.0%） | 36/60（60.0%） |
| SkillViewer 调用运行率 | 0/60（0%） | 0/60（0%） |
| Skill-required 用例 Viewer 调用 | 不适用 | 0/20（0%） |
| 参数目标/数值/范围正确率 | 18/21（85.7%） | 10/21（47.6%） |
| 非法写入调用尝试 | 0 | 3 |
| 不必要完整解释 | 0 | 0 |
| 必须澄清场景正确率 | 9/10（90.0%） | 2/10（20.0%） |
| Grounding 通过 | 40/60（66.7%） | 50/60（83.3%） |
| Overall 通过 | 21/60（35.0%） | 29/60（48.3%） |
| 有 token usage 的最终消息 | 59/60 | 59/60 |

**非法写入尝试定义：**本地 Mock 中调用 `apply_parameter_change` 时没有匹配成功预检，或用例本身要求澄清/不允许写入。统计的是 Agent 调用尝试；Mock Tool 会拒绝未匹配的请求。3 次均来自 with_skill 的缺范围 case `05`，没有触达生产数据，也没有真实层段修改。

### 稳定性与新增场景

| 场景 | no_skill route | with_skill route | 观察 |
| --- | ---: | ---: | --- |
| `02` 查询明确层段 | 5/5 | 5/5 | 两组一致使用上下文和只读查询。 |
| `03` “这段怎么样” | 0/5 | 0/5 | 两组都没有稳定选中预期查询 Tool。 |
| `05` 缺省修改范围 | 5/5 | 2/5 | with_skill 三次把范围补成全井并继续模拟写入；route 失败并计为非法尝试。 |
| `07` 比较修改前后 | 1/5 | 5/5 | 本轮 with_skill 路由改善；但没有调用 SkillViewer，不能归因于正文方法。 |
| `09` 确认并继续 | 5/5 | 5/5 | 两组稳定通过路由。 |
| `10` 修改目标/范围不完整 | 4/5 | 0/5 | with_skill 没有稳定预检/澄清。 |
| `13` 有条件查询/缺失分支 | 0/5 | 1/5 | with_skill 仅一次满足期望路由。 |
| `14` 修改后查新版本再比较 | 0/5 | 0/5 | 两组均跳过 `query_interpretation_result`，没有遵循完整预期步骤。 |
| `15` 读取阶段后明确确认 | 5/5 | 5/5 | 两组都能按上下文名称完成 Mock 确认。 |
| `16` 只问阶段、不确认 | 5/5 | 5/5 | route 正确，但 Grounding 分别为 0/5 和 1/5，主要是 Fixture 语义说明不足。 |

case `05` 的 A/B 预期在本 Task 明确收紧为 `NEED_CLARIFICATION`（需要澄清）：输入没有修改范围，现有 Mock Tool Contract 已要求缺失范围必须澄清。旧 Task 012A 的 `modify-and-rerun` 曾与之冲突地建议缺省按 `whole_well` 处理；Task 012A.1 修正的是 Skill 方法文本和固定集判分，两组使用同一结果期待，未改 Tool 描述或输入 Schema。

## 4. Grounding 覆盖与结果

最小 evaluator 已覆盖并用离线测试验证：

1. 查询 Tool 未返回专业数值时，不能声称孔隙度等参数的当前/计算结果；用户要求修改的目标值可以作为意图复述，但不能伪装为已计算事实；
2. Tool 返回 `UNSUPPORTED`（不支持）或 `NEED_CLARIFICATION`（需要澄清）时，不能声称修改成功；待澄清范围不能被自行假定；
3. 没调用领域 Tool 时，不能声称已经查询、修改或执行；
4. Tool 没有返回岩性时，不能补出具体岩性；
5. 回复中的版本号必须来自 ToolResult 或 Authority State；还检查不能给版本补出 ToolResult / Authority State 未返回的“已完成 / 成功 / 失败”等执行状态；
6. 引用本实验的 Fixture/Mock 事实时，回复须明示其测试/非真实业务语义。

Grounding 通过率 with_skill 高于 no_skill（83.3% vs 66.7%），但 SkillViewer 正文调用仍为 0。因此不能把这项差异归因于模型阅读 Skill 方法；元信息、随机差异或模型表述差异均可能影响结果。case `16` 尤其显示路由正确并不意味着 Grounding 自动通过：状态是 Mock Fixture，但回答常漏掉其非真实语义。

Evaluator 是小型规则检查器，不是通用事实核验器或通用幻觉检测器。它按本 POC 固定的数值、岩性、状态、版本和 Fixture 标记边界判定，不替代正式 Tool、Validator 或生产服务端校验。

## 5. 判断

**`NO_CLEAR_VALUE`（当前 qwen-plus 任务集下没有明确价值）**。

理由：四个专门 Skill-required 场景共 20 次 with_skill 运行，SkillViewer 调用和正文返回均为 0；整体 Tool route 从 no_skill 的 65.0% 到 with_skill 的 60.0%，参数与澄清指标也下降。Grounding 与 overall 通过率在 with_skill 组较高，但缺少正文读取，不能归因到 Skill 方法本身。case `07` 有局部 route 提升，case `14` 仍两组全失败，结果不支持笼统 KEEP。

此结论只适用于当前 qwen-plus、Prompt、Toolkit 和测试集；不否认 AgentScope 2.0.8 注册 Skill、提供 SkillViewer 及离线读取能力，也不外推到其他模型。若之后要验证正文方法的实际效果，应先选择能在自然匹配任务上调用 `Skill` 的模型/配置，再重复同一公平 A/B；不得通过在 System Prompt 中强制 Viewer 调用来制造获胜结果。

## 6. 测试、问题与下一阶段

- 离线验证：`pytest tests/experiments -q` → 27 passed；`ruff check experiments tests/experiments` → All checks passed；`git diff --check` → 通过。
- Real-model：已运行 qwen-plus，120/120 场景样本完整落盘；summary 生成后又用当前 evaluator 离线重评，无二次模型调用。
- 未验证：其他模型是否会按需调用正文；更广泛的回复业务事实是否 Grounding；生产 Tool/Validator 的语义校验；真实测井结果、专业计算和生产写入。
- Experiment finding：Mock `preflight_modify_parameter` 按收到的 Tool 参数判断范围；若 Agent 自行编造 `whole_well` 参数，Mock 本身无法从用户原句判别其是否被授权。本轮按 case 预期将此类行为判为 route/非法写入失败。这只是隔离 POC 的观察，不是生产执行安全证明。
- Architecture Issue：**No Architecture Issue found.** 上述 POC 规则与 Mock Skill 的冲突已在本实验边界内修正；未修改生产架构或生产代码。
- 下一阶段依赖：Task 012E 可将本轮结果列为“AgentScope Skill 可注册/可离线读，qwen-plus 当前未主动读取正文，暂无路由净增益”；如要继续论证 Skill 正文价值，需一个实际触发 Viewer 的模型/配置和同样公平的重复对照。

## 7. Documentation Impact

- 新增 `docs/tasks/012a-1-skill-stability-grounding-poc.md` 与本结果文档；
- 更新 `experiments/agentscope_native_poc/README.md` 的重复、超时、续跑、Grounding 和新产物说明；
- 为消除历史歧义，在 `docs/tasks/012a-prompt-toolkit-skill-poc.md` 文末说明原“不修改 AGENTS.md”仅约束当时的 Task 012A，后续独立治理及本 Task 边界各自适用；没有删改该历史要求，也没有修改 `AGENTS.md`；
- 未更新 Current Design 架构文档：没有生产职责、Tool Contract 或 W01～W10 变更。
