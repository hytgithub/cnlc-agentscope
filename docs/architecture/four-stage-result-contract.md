# Task 11D — Four-Stage Result Contract

## 1. 定位与事实来源

`StageResultView`（阶段结果展示视图）是应用层的只读查询投影，用于阶段确认页和进度查询。
它不是新的业务实体、Revision（版本）或持久化真相。投影只读取以下已有事实：

- `InterpretationState`：井资料摘要和 W01～W10 的结构化结果；
- `StageRun`：阶段、生命周期、有效性、逻辑输入输出引用和诊断；
- `Execution`：执行状态、候选报告、运行模式和更新时间；
- `InterpretationTask`：井归属和 `current_execution_id`（当前执行指针）；
- `DatasetRevision` / `Report`：当前通过 StageRun 逻辑引用或 Execution 候选报告体现，
  后续独立查询端口接入时仍只投影小型事实。

查询必须显式提供 `task_id`、`execution_id` 和 `stage_run_id`。Projector（投影器）先验证
Task 与 Execution 归属，再只在该 Execution 的状态快照中查找 StageRun。新工作快照中可包含
来源 Execution 的 StageRun 副本；它仍保留原 `StageRun.execution_id`，视图中的
`execution_id` 表示本次查询和有效性判断所使用的 Execution 快照。

`StageOrchestrator`（阶段编排器）负责执行、暂停和确认；`StageResultProjector`（阶段结果投影器）
负责展示数据拼装。`get_progress()` 复用 Projector 产生当前 `stage_result`，不在编排器中维护第二套
展示规则。调用方也可用显式三元标识查询历史阶段。

## 2. 统一展示契约

`StageResultView` 至少包含：

| 字段 | 含义 |
| --- | --- |
| `task_id`、`well_id` | Task 与井的可信归属。 |
| `execution_id`、`stage_run_id` | 查询快照与阶段运行标识。 |
| `stage` | `DATA_DECODE`（数据解编）、`PREPROCESS`（预处理）、`INTERPRET`（解释）或 `REPORT`（报告）。 |
| `status`、`validity` | `StageRunStatus`（阶段状态）与 `StageValidity`（结果有效性）。 |
| `headline`、`summary` | 由持久事实确定性生成的简短标题和摘要。 |
| `metrics`、`items` | 有界 JSON 指标和最多 50 行的小型条目。 |
| `warnings`、`conflicts`、`missing_items` | 已有诊断、冲突和明确缺失项，稳定去重并限制数量。 |
| `input_refs`、`output_refs` | StageRun 的逻辑引用；不得是本地文件路径或结果正文。 |
| `can_confirm`、`confirmation_blockers` | 确认能力和结构化阻断原因。 |
| `tool_runs` | 当前业务阶段内实际 ToolRun 的有界审计投影，最多 50 条。 |
| `updated_at` | Execution / StageRun 相关事实中的最新时间。 |

投影代码按字段白名单读取专业结果，不透传任意 `StageResult.result`。文本、列表、嵌套深度和
序列化体积都有上限；这些限制同时由 `StageResultView` 模型校验，避免其他调用方绕开 Projector。

## 3. 四阶段输入、输出和确认摘要

### 3.1 DECODE（数据解编，W01）

| 类别 | 契约 |
| --- | --- |
| 输入 | InputVersion 逻辑引用，或现有受控井资料入口。 |
| 输出 | Dataset Revision 逻辑引用；正式 DatasetRevision 存在时使用其 ID。 |
| 展示摘要 | 井号、井名、深度最小值/最大值、深度单位和 reference、曲线名与单位、曲线数量、样本点数量、可确定的统一采样间隔、必需曲线缺失、数据来源或来源引用、InputVersion 与 DatasetRevision 引用、告警。 |
| 用户确认依据 | 井归属、深度覆盖、曲线清单、必需曲线是否齐全以及数据来源是否符合预期。 |
| 不可展示 | 完整 `raw_data`、完整 `depths`、任何曲线 `values`、本地文件路径。 |
| 后续 Tool 边界 | 解编 Tool 可增加明确的小型统计或正式 DatasetRevision 引用；Projector 不重新解编文件。 |

统一采样间隔只在现有深度轴确实等间隔时展示。深度轴不等间隔或点数不足时明确说明无法确定，
不推算重采样结果。

### 3.2 PREPROCESS（预处理与质量控制，W02～W03）

| 类别 | 契约 |
| --- | --- |
| 输入 | 已确认 Dataset 引用和预处理参数版本引用。 |
| 输出 | Preprocess Result 逻辑引用。 |
| 展示摘要 | QC 状态、输入/输出曲线数、输入/输出缺失样本计数、工具明确返回的异常统计、实际操作、请求采样间隔、是否实际重采样、结果引用和告警。 |
| 用户确认依据 | QC 结论、数据是否发生预期处理、重采样是否真实执行以及工具诊断。 |
| 不可展示 | 完整输入或输出曲线、完整深度轴、预处理文件路径、未经 Tool 提供的异常统计。 |
| 后续 Tool 边界 | 预处理 Tool 应通过正式输出契约提供操作清单和统计；缺少的统计显示“当前工具未提供该统计”。 |

缺失样本计数只统计当前状态中已有的 `None`；异常值、离群点或专业质量等级必须由 Tool 明确返回，
Projector 不自行设阈值。

### 3.3 INTERPRET（测井解释，W04～W10）

| 类别 | 契约 |
| --- | --- |
| 输入 | 已确认 Dataset、预处理结果和参数版本逻辑引用。 |
| 输出 | Interpretation Result 逻辑引用。 |
| 展示摘要 | 已有岩性、物性、流体、层分类、层段、验证、最终检查、冲突、告警、缺失证据和证据摘要。 |
| 用户确认依据 | 各专业结构化结论、层段表、验证状态、证据、冲突及限制。 |
| 不可展示 | 曲线数组、由展示层新算出的孔隙度/渗透率/饱和度/有效厚度、LLM 新生成的专业事实。 |
| 后续 Tool 边界 | 专业 Tool 负责正式计算并返回结构化结果；Projector 只读取字段白名单。 |

层段表最多 50 行，只投影已有的顶部、底部、总厚度、有效厚度、岩性、流体或层分类以及已有关键
物性。缺失字段保持缺失，不用相邻层界或其他阶段结果补算。测量值原样保留 `value` 和 `unit`。

### 3.4 REPORT（报告生成）

| 类别 | 契约 |
| --- | --- |
| 输入 | 已确认解释结果和报告配置逻辑引用。 |
| 输出 | Report Revision 逻辑引用及 Execution 上的候选 Markdown。 |
| 展示摘要 | `READY`（正式报告）、`CANDIDATE`（候选报告）或 `UNAVAILABLE`（尚无报告）、报告样式、候选正文是否存在、已有核心结论摘要、层段数、告警和报告引用。 |
| 用户确认依据 | 报告候选是否生成、所用样式、包含的层段数、已有最终检查/验证/分类摘要及告警。 |
| 不可展示 | `StageResultView` 中的完整 Markdown、曲线数组、本地文件路径。 |
| 后续 Tool 边界 | ReportAssembler 继续生成候选正文；正式 Report Revision 接入后只增加稳定引用和小型元数据。 |

当 REPORT StageRun 为 `WAITING_CONFIRM`（等待确认）且候选 Markdown 已存在时，视图标记
`CANDIDATE`（候选报告）。只有 Execution 已进入终态且正文存在时才标记 `READY`（正式报告）。

## 4. 确认判定

`can_confirm=true` 必须同时满足以下全部持久事实：

1. StageRun.status 为 `WAITING_CONFIRM`（等待确认）；
2. StageRun.validity 为 `CURRENT`（当前有效）；
3. Execution.status 为 `WAITING_CONFIRMATION`（等待阶段确认）；
4. Execution 是 Task.current_execution_id 指向的当前执行。

任何条件不满足时 `can_confirm=false`，并提供一个或多个阻断码：

| 阻断码 | 中文含义 |
| --- | --- |
| `EXECUTION_NOT_CURRENT` | 该结果属于历史执行或当前工作指针已经移动。 |
| `EXECUTION_NOT_WAITING_CONFIRMATION` | Execution 当前不处于等待阶段确认状态。 |
| `STAGE_NOT_WAITING_CONFIRM` | StageRun 当前不处于等待确认状态。 |
| `STAGE_RESULT_STALE` | 当前工作快照中的阶段结果已经失效。 |

历史 Execution 可以查询并展示当时的完整事实，但其 `can_confirm` 必须为 false。新工作快照中的
STALE（已失效）来源结果也可展示，用于说明失效依据，但不能确认。

## 5. 隔离和大数据保护

- Projector 不读取 ActiveContext（活动上下文），不从会话推断 Task 或井。
- Execution 必须属于显式 Task，Execution 快照中的井必须与 Task 一致；不匹配按不存在拒绝。
- StageRun 必须存在于所查 Execution 的持久快照且属于同一 Task。
- `metrics`、`items` 禁止出现 `raw_data`、`processed_data`、`depths`、`values`、
  `curve_values`、`markdown` 或 `candidate_markdown` 键。
- `tool_runs` 只投影持久化的有界 input/output snapshot；不加载完整 Tool 输入、provider 响应或报告正文。
- 条目与诊断列表最多 50 项，JSON 嵌套深度最多 5 层，展示正文最多 128 KiB。
- 允许展示曲线名、单位、数量、已有 min/max、已有测量值以及逻辑 refs。

上述限制保护查询和确认页面，不改变 InterpretationState、Execution 或正式报告中的原始事实。

## 6. 当前实现边界

本任务不增加前端 UI、Trace 事件、LLM Prompt、真实公司 API、专业算法或曲线数据副本。
确认和继续执行仍由 StageOrchestrator 与仓储事务处理；Projector 不修改任何状态。

ToolRun 的阶段过滤与审计边界详见
[四阶段 Tool 映射与可审计 Mock 集成](four-stage-tool-mapping.md)。

No Architecture Issue found.
