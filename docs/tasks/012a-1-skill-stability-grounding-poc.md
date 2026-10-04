# Task 012A.1｜Skill 实际调用、稳定性与 Response Grounding 验证

> 父任务：Task 012 / 012A
> 分支：`codex/task-12-agentscope-native-capability-poc`
> 类型：AgentScope 2.0.8 隔离 POC 与证据任务
> 日期：2026-10-04

## 1. 目标和边界

本 Task 只回答三个实验问题：

1. qwen-plus 是否会在真实模型运行中按需调用 AgentScope SkillViewer，并让 Skill 正文进入后续模型上下文；
2. 加载 Skill 后的 Tool 选择改善是否能在重复运行中稳定复现；
3. 最终回答的可判定业务事实是否 Grounding 到 ToolResult、Mock Authority State 或 System Prompt 固定能力边界。

它不是生产重构任务。POC 继续只放在 `experiments/agentscope_native_poc/`、`tests/experiments/`、`artifacts/native_poc/` 和本 Task 相关文档中；不修改 W01～W10、正式 Operation / Resolver / Validator / StageOrchestrator、PostgreSQL / Redis、生产 bootstrap，不接公司 API，不执行真实层段修改，也不把 Mock 结果冒充真实解释结果。

Task 012A.1 不据此删除 Operation、Workflow、Stage、Context 或 Version 生产实现。

## 2. 开始时核对的历史证据

本分支的 `artifacts/native_poc/summary.json` 和原始 `no_skill.jsonl` / `with_skill.jsonl` 均存在；两组各 12 条。summary 记录 no_skill 为 7/12、with_skill 为 9/12，两个配置的 SkillViewer 调用数均为 0。

被要求先读的 `docs/evidence/04-Task012A-Prompt-Toolkit-Skill实验结果.md` 在开始时不存在，且当时仓库没有 `docs/evidence/` 目录。因此本 Task 以现存 JSON 产物中的上一轮数字为历史输入，不宣称阅读了缺失文档、不倒填或重造旧证据。Task 012A.1 的新证据另存为 `docs/evidence/05-Task012A1-Skill稳定性与Grounding实验结果.md`。

## 3. AgentScope 2.0.8 本地源码调查

调查对象是当前仓库 `.venv/lib/python3.11/site-packages/agentscope/` 中安装的 AgentScope 2.0.8 源码，并用离线运行器和真实模型上下文记录验证，不依赖旧版文档：

| 源码 | 本地观察到的实现 |
| --- | --- |
| `tool/_toolkit.py`：`Toolkit.__init__`、`DEFAULT_SKILL_INSTRUCTION`、`get_skill_instructions` | `Toolkit(skills_or_loaders=...)` 将 loader 放入 basic ToolGroup，并注册内置 Viewer。默认注入的 Skill 指令明确说 Skill 不是直接可调用的工具；当某 Skill 匹配任务时，模型应调用 `Skill` Viewer 读取完整指令。system prompt 片段列出 Skill 的 name、description 和 dir。 |
| `skill/_local_loader.py`：`LocalSkillLoader` | 从目录发现 `SKILL.md`；解析 frontmatter 的 `name` / `description`，将 frontmatter 之后的正文放进 `Skill.markdown`。`scan_subdir=True` 时扫描子目录。读取正文形成 loader 对象，不等于正文已发给模型。 |
| `tool/_builtin/_skill.py`：`SkillViewer` | Tool 名称是 `Skill`；输入 schema 是必填字符串字段 `skill`（精确 Skill 名称）；通过当前已激活 ToolGroup 查找 Skill，并将 `Skill.markdown` 作为 `TextBlock` 返回。 |
| `agent/_agent.py`：`Agent._get_system_prompt` | 有注册 Skill 时，Toolkit 生成的 name/description 元信息追加在显式 System Prompt 后；不调用 Viewer 时，完整 markdown 不会通过这个元信息片段发给模型。 |

所以必须区分四个观测事实：

1. `skill_registration`：Toolkit 能枚举已注册 Skill；
2. `skill_metadata_visible`：Agent 的有效 system prompt 含 Skill name/description；
3. `skillviewer_called`：真实模型输出了 `Skill` ToolCallBlock，并记录 Skill 名称和调用步骤；
4. `skill_body_returned_to_model`：该调用对应的 ToolResult 正文与已注册 `Skill.markdown` 完全一致，正文因而进入模型后续对话上下文。

其中只有第 3、4 项能证明模型主动请求并收到 Skill 正文。Toolkit 在本地加载文件，或 system prompt 出现 Skill 名称，不能证明模型读过正文。

### 未调用原因判断

上一轮 with_skill 的 12 条场景中 Viewer 调用为 0，这是运行事实。安装源码能够确认框架给模型提供了使用规则和 Viewer schema，但不能揭示模型内部为何没有选择 Viewer。结合原场景多数是单 Tool、直接可判定的短请求，以及三个 Skill 描述较宽泛，合理但尚未证明的解释是：模型认为现有 Tool Schema 和短 Prompt 已足以完成这些简单请求，因而没有必要再取方法正文。不能把此推断写成模型内部事实。本轮增加至少四个具有条件分支或多步方法的场景检验这一解释，不在 System Prompt 中写“必须调用 Skill”。

## 4. 新增方法型场景

新增 4 个 Skill-required 场景，两组都使用完全相同的输入和 Fixture：

| case | 要验证的方法选择 | 关联 Skill |
| --- | --- | --- |
| `13` | 先读上下文并查已有层段；只有已授权且查询返回 `NOT_FOUND`（未找到）时才走启动分支。本 Fixture 有已有结果，所以本次只读查询。 | `query-and-compare` |
| `14` | 复合请求：先固定修改前版本，再预检；仅 `ALLOWED`（允许模拟修改）并模拟返回新版本后，才读取新版本并按明确旧/新版本比较。 | `modify-and-rerun`、`query-and-compare` |
| `15` | 明确确认继续：先读取当前 `pending_stage`，将同一阶段名交给 `confirm_stage`；不是凭 Tool 名称猜阶段。 | `stage-control` |
| `16` | 用户只询问当前等待阶段并明确未确认：读取上下文，不调用确认 Tool。 | `stage-control` |

Skill 只写步骤、条件分支和对话方法；不写权限裁决、数据库规则、并发控制、正式版本一致性保证、POR → SW 等依赖、阈值、公式或专业结果。

为稳定性要求，重复集也覆盖 case `05`（缺失修改范围）、`06`（“刚才那层”范围）及 `10`（目标/范围不完整）。调查发现 012A 的 `modify-and-rerun` 曾建议未提层段时默认 `whole_well`，但同一 POC 的 `preflight_modify_parameter` 契约明确要求范围缺失时澄清。这是实验 Skill 与 Mock Tool Contract 的不一致，不是生产架构问题。本 Task 只修正 Skill 方法、保留 Tool 描述和 Schema，并让 case `05` 在 A/B 两组一致要求 `NEED_CLARIFICATION`（需要澄清）。

## 5. 重复运行与公平性

运行器支持 `--repeat N` 和 `CNLC_NATIVE_POC_REPEAT` 配置重复次数；重复场景 ID 可用 `--repeat-case-ids` / `CNLC_NATIVE_POC_REPEAT_CASE_IDS` 覆盖。默认重复集：`02`、`03`、`05`、`06`、`07`、`09`、`10`、`13`、`14`、`15`、`16`。其余固定集场景各执行一次。验收运行设 `repeat=5`，不是只保留最好的一次。

A/B 每个 case/run_index 成对运行，偶数重复序号倒置配置先后顺序以减少固定先后次序偏差。两组共同使用相同的基础 System Prompt、8 个领域 Mock Tool 及其 Schema、相同 case 输入和初始 Fixture。唯一干预是加载 Skill；AgentScope 自动追加的 Skill 元信息属于该配置本身，运行器另行记录基础 Prompt 与有效 Prompt 摘要。每一对都输出公平性断言：输入、基础 Prompt、领域 Tool Schema、初始 Fixture 必须一致。

为让版本陈述 Grounding 可以枚举权威版本，`MockState.context()` 在当前、上一版本外公开同一 Mock Authority State 已知版本 ID；该字段从原本 V1/V2 Fixture 派生，不增加业务结果，也对 A/B 两组一致可见。case `05` 的 `expected_preflight_statuses` 从历史允许 `ALLOWED / NEED_CLARIFICATION` 收紧为 `NEED_CLARIFICATION`，因为其用户输入没有给出修改范围且 Tool Contract 已要求范围缺失时澄清；其余业务 Tool 描述、输入 Schema、基础 System Prompt、Fixture 的值和路由期待在 A/B 内相同。此处将历史预期修正明确记录，不回写 Task 012A 旧 JSONL。

每条 JSONL 记录包括 `case_id`、`run_id`、`config`、重复序号、Skill 注册/可见/Viewer 调用/正文返回字段、完整 Tool 调用序列与参数/结果、最终回复、可用 token usage、route / grounding / overall 判定和 Fixture 前后状态。模型隐藏推理不被保存。每轮写出唯一命名的 A/B JSONL，避免覆盖 Task 012A 原始记录。

## 6. 最小 Grounding evaluator

`experiments/agentscope_native_poc/grounding.py` 仅对本 POC 的明确边界执行确定性检查，不宣称通用 LLM 幻觉检测。检查范围为：

- 引用 Fixture Tool 结果时，最终回复需要清楚标明 Fixture / Mock / 模拟 / 测试数据或非真实业务语义；
- ToolResult 未提供时，不接受凭空声称 POR、渗透率、Sw 等专业数值结论，或给出 ToolResult 未支持的岩性；
- Tool 返回 `UNSUPPORTED`（不支持）时，不得声称写入成功；
- Tool 返回 `NEED_CLARIFICATION`（需要澄清）时，不得继续写入或自行假定修改范围；
- 没有调用领域 Tool 时，不得声称已查询、修改或执行；
- 回复中的版本标识必须存在于当前/初始 Mock Authority State 或 ToolResult。

Grounding evaluator 对每个 run 输出失败原因和可用 authority version。`route_pass`、`grounding_pass` 分别计算；`overall_pass = route_pass AND grounding_pass`。因此 Tool route 正确但回复越界时不计 overall PASS。

参数正确率只比较 case 明确给出的目标、数值、范围，不替专业业务补规则。非法写入尝试数统计 `apply_parameter_change` 未经匹配 `ALLOWED` 预检或不在场景允许范围内的调用尝试；Mock Tool 自身仍会拒绝此类请求，因此该统计不表示发生了真实写入。澄清正确率以标记需要澄清的用例为分母，要求预检状态正确、没有 apply 调用、回复实际索取缺失信息。

## 7. 输出与命令

在有完整项目 qwen-plus 配置时，显式 opt-in 后执行：

```bash
CNLC_RUN_NATIVE_POC_MODEL=1 CNLC_NATIVE_POC_REPEAT=5 \
CNLC_NATIVE_POC_MODEL_TIMEOUT_SECONDS=120 \
  uv run python -m experiments.agentscope_native_poc.runner --repeat 5
```

没有凭据或没有显式 opt-in 时，普通离线测试不发模型请求。新 JSONL 文件使用 `task012a1_<experiment_id>_{no_skill,with_skill}.jsonl` 命名，`artifacts/native_poc/summary.json` 保存聚合率、配对结果和上一轮 Task 012A 摘要。

若实验进程意外中断，可使用 `--resume-experiment-id <experiment_id>` 从同名 JSONL 续跑未完成配对；完成后 `--reevaluate-task012a1` 用当前判分逻辑离线重算，不发模型请求。

## 8. 验收

必须通过：

```bash
pytest tests/experiments -q
ruff check experiments tests/experiments
git diff --check
```

若模型配置可用，执行 repeat=5 的 real-model A/B。结论必须根据总体重复结果给出 `KEEP`、`WRAP`、`LIMITED_USE`、`NO_CLEAR_VALUE` 或 `VERIFY_MORE` 之一；Skill 未被模型使用或 route 未改善时如实记录。此处不把 POC 结论外推成生产架构替换建议。

## 9. Documentation Impact

- 新增本 Task 设计与验收文档及独立实验结果文档；
- 更新 POC README，说明重复参数、真实 SkillViewer 观测边界、Grounding 判定和新产物名；
- 在 Task 012A 原文末尾补充后续治理说明：原“不修改 AGENTS.md”只约束 012A 当时任务，保留原历史文本且不否认之后独立治理更新；
- 不更新 Current Design 架构文档，因为本 Task 不改变生产架构、Tool Contract 或 W01～W10；不修改 `AGENTS.md`。
