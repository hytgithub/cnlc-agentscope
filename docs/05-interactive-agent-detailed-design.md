# 交互式测井解释智能体详细设计与 E2E 验收基线

版本：v1.0  
目标：作为 Codex 开始实施代码前的详细设计基线  
范围：一期可交互式 Demo，重点验证自然语言交互、参数修改、局部/全量重跑、工具过程展示、报告生成与结果追溯。

---

## 1. 建设目标

一期目标不是一次性实现全部真实测井算法，而是先建立一个完整、可验证的交互闭环：

```text
用户自然语言输入
    ↓
AgentScope 交互层（渐进升级现有 LoggingInterpretationDemoAgent）
    ↓
意图识别 / 参数提取
    ↓
任务级 Tool
    ↓
TaskService
    ↓
DependencyResolver
    ↓
ExecutionPlan
    ↓
复用状态装配
    ↓
现有业务 MainAgent → InterpretationWorkflow → W01～W10
    ↓
Logical Tool / Mock / Virtual / Real
    ↓
解释结果
    ↓
独立 REPORT：ReportAssembler / ReportGenerator
```

一期重点验证：

1. 用户能够通过自然语言发起一口井的解释任务；
2. 用户能够修改采样间隔、预测模型、POR/PERM 等解释参数；
3. 系统能够自动判断局部重跑范围；
4. 系统能够执行全量重跑；
5. 前端能够展示阶段状态与工具调用轨迹；
6. 工具尚未实现时可以使用 Mock；
7. 第三方 Heavy API 返回结果可以映射为 Virtual Tool；
8. 每次重新解释生成新的 Execution 与报告，不覆盖历史结果；
9. 用户可以查询当前执行进度；
10. 为后续接入真实第三方 Prediction API / Report API 保留稳定接口。

---

## 2. 总体设计原则

### 2.1 Agent 负责交互，不负责专业流程

AgentScope 交互层负责：

- 理解自然语言；
- 识别用户意图；
- 提取参数；
- 识别当前操作对象；
- 调用任务级 Tool；
- 查询任务状态并回复用户。

Agent 不负责：

- 自行决定四阶段执行顺序；
- 自行判断哪些阶段失效；
- 自行维护真实 Task 状态；
- 直接操作底层专业 Tool；
- 自行构造测井解释结果。

核心原则：

> ReAct for Interaction，Workflow for Execution。

现有 `agents/main_agent.py::MainAgent` 是业务编排类，不是 AgentScope ReAct Agent；真正的 Web 外壳是 `LoggingInterpretationDemoAgent`。目标是改造该交互外壳并增加任务级命令，继续由业务 MainAgent 启动 `InterpretationWorkflow`。`InterpretationAgent` 与 `ValidationAgent` 保持 W06/W07、W09 的现有职责。

### 2.2 Workflow 负责确定性执行

四阶段是 Execution、局部重跑、状态展示的上层视图，对现有 W01～W10 与报告链作如下映射：

```text
DATA_DECODE
└── W01
PREPROCESS
├── W02
└── W03
INTERPRET
├── W04、W05、W06、W07、W08、W09
└── W10：最终一致性 / 结构检查
REPORT
└── W01～W10 完成后，由 ReportAssembler / ReportGenerator 独立承担
```

W01～W10 保持现有业务顺序，不新建第二套四阶段 Workflow。当前 `InterpretationWorkflow.run(...)` 总从 W01 开始；局部重跑目标是对其入口做受控的最小扩展，而不是重写处理器。

每一次实际执行由 `ExecutionPlan` 决定：

```text
RUN / REUSE / SKIP
```

### 2.3 Tool 属于阶段内部能力

Tool 不等同于一级 Workflow 节点。

四阶段可聚合现有步骤和 Tool；下文列出的 Logical ToolSet 是目标候选，不代表当前 Demo 均已实现。现状清单以 `docs/06-interactive-agent-gap-analysis.md` 为准。

Logical Tool 可以是：

- `REAL`：真实原子 Tool；
- `MOCK`：Demo 模拟结果；
- `VIRTUAL`：从第三方 Heavy API 返回结果中映射出来；
- `DERIVED`：根据已有结果通过确定性逻辑推导得到。

---

## 3. 核心数据模型

目标保留 6 个核心业务对象，但不要求一期一次性全部建表或实现。Task 01 先扩展现有 Task 持久化并新增 Execution；Task 02 增加 Input Version 与 Override；StageRun、ToolRun 和 Artifact 引用按 Gap Analysis 后续 Task 渐进实施。

### 3.1 InterpretationTask

代表一口井的一次持续交互解释任务。

同一口井在一次持续交互过程中，第一次解释、修改采样间隔、修改 POR/PERM、更换预测模型、全量重跑，都属于同一个 Task。

### 3.2 Execution

代表一次真正执行解释流程的运行实例。

每次真正触发重跑，都创建一个新的 Execution：

```text
EXEC_001  第一次完整解释
EXEC_002  修改 POR/PERM 后重跑
EXEC_003  更换预测模型后重跑
EXEC_004  全量重跑
```

历史 Execution 不覆盖、不修改。

### 3.3 StageRun

目标上每个 Execution 有四阶段状态。首先按固定映射聚合现有 `InterpretationState.executions` 中的 `StepExecution`，REPORT 由现有报告链状态补足；需要持久化复用来源和阶段生命周期时再补 StageRun 记录：

```text
DATA_DECODE
PREPROCESS
INTERPRET
REPORT
```

局部重跑示例：

```text
EXEC_002

DATA_DECODE   REUSED
PREPROCESS    REUSED
INTERPRET     COMPLETED
REPORT        COMPLETED
```

### 3.4 ToolRun

后续在现有 `ToolCaller` 调用边界增加 ToolRun，记录业务工具轨迹，不把已有日志 Trace 或 AgentScope 高层 Tool 消息误作持久 ToolRun。字段包括：

- tool_code；
- stage；
- status；
- execution_mode；
- input_snapshot；
- output_snapshot；
- source；
- source_external_call_id；
- started_at；
- finished_at；
- error。

### 3.5 InterpretationOverride

用户修改的解释参数采用 Override，不修改原始数据。

例如：

```text
OVERRIDE_V002
POR  = 0.16
PERM = 0.16
source = USER
```

### 3.6 Artifact

目标上统一表示流程产生的重要文件或结果：

```text
Decoded Data Artifact
Preprocessed Input Artifact
Prediction Input Artifact
Prediction Output Artifact
Normalized Interpretation Result
Report Artifact
```

一期先记录受控 URI/路径、类型、输入摘要及来源 Execution 等最小 Artifact 引用；上传原文件必须可恢复供重跑使用，不立即建设通用 Artifact 大表。

---

## 4. 四阶段 Execution 视图与契约

以下阶段边界不改变现有十步 Workflow。当前 W03 只回放 Mock QC 并将 `raw_data` 原样赋给 `processed_data`，尚无真正重采样；采样间隔的目标行为不得被描述为已实现。W10 仅做最终一致性/结构检查，REPORT 在其后独立运行。

### 4.1 DATA_DECODE

目标：把数据库数据或上传文件转换为系统可识别的标准井数据。

输入：

```text
well_id
或
source_file
```

Logical Tool：

```text
load_well_data
parse_well_file
recognize_materials
recognize_curves
identify_curve_roles
extract_well_metadata
```

输出：

```text
DecodedWellData
```

核心解释输入候选：

```text
DEPTH
GR
CAL
AC
DEN
CNL
RT
SP
```

历史解释/派生数据：

```text
POR
PERM
SH
SW
SOG
```

默认不得直接作为新的原始预测输入，应记录为 `REFERENCE_RESULT / HISTORICAL_RESULT / DERIVED_RESULT`。

可复用条件：原始文件、数据源、well_id、曲线映射、曲线角色均未变化。

### 4.2 PREPROCESS

目标：将 DecodedWellData 处理成可用于智能处理的稳定输入版本。

输入：

```text
DecodedWellData
+
PreprocessConfig
```

一期重点参数：

```text
sampling_interval
```

Logical Tool：

```text
check_completeness
check_curve_quality
analyze_caliper
crossplot_analysis
standardize_curve_names
convert_units
resample_curves
align_depth
build_interpretation_input
```

输出：

```text
InterpretationInputVersion
```

至少包含：

- input_version；
- well_id；
- sampling_interval；
- depth_range；
- input_curves；
- reference_curves；
- preprocessed_file_path；
- source_execution_id。

可复用条件：DecodedWellData、sampling_interval、预处理配置均未变化。

### 4.3 INTERPRET

目标：基于输入数据、用户 Override 和模型选择完成智能解释。

输入：

```text
InterpretationInputVersion
+
InterpretationOverride
+
PredictionModel
```

Logical Tool：

```text
apply_interpretation_overrides
select_prediction_model
build_prediction_input
invoke_prediction_model
parse_prediction_output
identify_lithology
evaluate_petrophysics
calculate_sw
identify_fluid
classify_layer
merge_intervals
multi_source_review
```

未来第三方 Heavy API 预计采用文件交互，具体协议尚待确认，因此目标上增加：

```text
PredictionInputBuilder
```

用于：

```text
InterpretationInputVersion
+
Override
+
Model Config
        ↓
PredictionInputArtifact
```

现有 `ModelGateway`（`MockModelGateway` / `OpenAICompatibleModelGateway`）供 qwen-plus 或 Mock LLM 使用，继续支持 W06/W07 等能力。未来专业预测的三种模型通过独立 `PredictionProvider` / `PredictionGateway` 管理：

```text
PredictionGateway
    ├─ MODEL_A Adapter
    ├─ MODEL_B Adapter
    └─ MODEL_C Adapter
```

用户说“换成模型B”时，新 Execution 修改 `prediction_model=MODEL_B`；不改变 qwen-plus 的 `ModelGateway` 配置。两个 Gateway 不合并，也不重构已验收的模型访问层。

第三方输出文件统一经：

```text
PredictionResultParser
```

转换为：

```text
NormalizedInterpretationResult
```

再映射为 Logical Tool 结果：

```text
identify_lithology        VIRTUAL
evaluate_petrophysics     VIRTUAL
calculate_sw              VIRTUAL
identify_fluid            VIRTUAL
classify_layer            VIRTUAL
merge_intervals           VIRTUAL
```

如果真实 Heavy API 尚未接入，则使用 `MOCK`。

最终输出：

```text
InterpretationResult
```

可复用条件：InputVersion、Override、PredictionModel、智能处理配置均未变化。

### 4.4 REPORT

目标：在 W10 完成后，根据 InterpretationResult 生成最终解释报告。当前由 `ReportAssembler` / `ReportGenerator` 读取 `InterpretationState` 生成 JSON / Markdown；Report API 是后续 Provider，不是现有能力。

输入：

```text
InterpretationResult
+
ReportConfig
```

Logical Tool：

```text
prepare_report_payload
invoke_report_api
resolve_report_artifact
```

上述是后续外部 Report API 的候选逻辑能力；一期先包装现有报告链，并按 execution_id 保存新旧报告。

输出：

```text
ReportArtifact
```

历史报告不可覆盖：

```text
EXEC_001 → REPORT_001
EXEC_002 → REPORT_002
EXEC_003 → REPORT_003
```

---

## 5. 参数影响与局部重跑规则

| 变化内容 | DATA_DECODE | PREPROCESS | INTERPRET | REPORT |
|---|---|---|---|---|
| 原始文件/井数据 | RUN | RUN | RUN | RUN |
| 曲线映射/角色 | RUN | RUN | RUN | RUN |
| 采样间隔 | REUSE | RUN | RUN | RUN |
| 预处理配置 | REUSE | RUN | RUN | RUN |
| POR/PERM Override | REUSE | REUSE | RUN | RUN |
| 预测模型 | REUSE | REUSE | RUN | RUN |
| 智能解释参数 | REUSE | REUSE | RUN | RUN |
| 报告模板 | REUSE | REUSE | REUSE | RUN |
| 用户要求全部重跑 | RUN | RUN | RUN | RUN |

---

## 6. ExecutionPlan

计划由确定性代码生成，执行目标链路是 `ExecutionPlan → 复用状态装配 → 现有业务 MainAgent → InterpretationWorkflow.run(...) → W01～W10 → 独立 REPORT`。复用的业务结果需验证来源与输入版本；仅在现有 Workflow 入口增加受控起点能力，不新增四阶段 WorkflowRunner。W10 仍检查有效的完整十步结果。

首次解释：

```text
DATA_DECODE   RUN
PREPROCESS    RUN
INTERPRET     RUN
REPORT        RUN
```

修改采样间隔：

```text
DATA_DECODE   REUSE
PREPROCESS    RUN
INTERPRET     RUN
REPORT        RUN
```

修改 POR/PERM：

```text
DATA_DECODE   REUSE
PREPROCESS    REUSE
INTERPRET     RUN
REPORT        RUN
```

更换模型：

```text
DATA_DECODE   REUSE
PREPROCESS    REUSE
INTERPRET     RUN
REPORT        RUN
```

仅重新生成报告：

```text
DATA_DECODE   REUSE
PREPROCESS    REUSE
INTERPRET     REUSE
REPORT        RUN
```

---

## 7. 状态模型

Execution Status：

```text
CREATED
QUEUED
RUNNING
PAUSE_REQUESTED
PAUSED
COMPLETED
FAILED
CANCELLED
```

Stage Status：

```text
PENDING
RUNNING
COMPLETED
REUSED
SKIPPED
FAILED
```

Tool Status：

```text
PENDING
RUNNING
COMPLETED
FAILED
SKIPPED
```

Tool Execution Mode：

```text
REAL
MOCK
VIRTUAL
DERIVED
```

一期暂停语义：

> 当前正在执行的 Stage 完成后停止，不继续执行下游 Stage。

---

## 8. AgentScope 交互层与现有业务 MainAgent

目标是在现有 `LoggingInterpretationDemoAgent` 上渐进增加受约束的 ReAct 交互。当前上传 Middleware 直接调用唯一 `run_well_interpretation` Tool，并未通过模型识别“修改/状态查询”意图；以下任务级 Tool 是未来能力。现有业务 MainAgent 继续启动和协调 Workflow，不需改写成 ReAct Agent。

Agent Task Tools：

```text
start_interpretation
modify_and_rerun
full_rerun
get_task_status
pause_task
resume_task
get_report
```

AgentScope 交互层不直接暴露底层专业工具。

核心接口：

```text
modify_and_rerun(
    task_id,
    changes,
    rerun_scope=AUTO
)
```

Agent 意图至少支持：

```text
START_INTERPRETATION
MODIFY_AND_RERUN
FULL_RERUN
QUERY_STATUS
PAUSE
RESUME
GET_REPORT
KNOWLEDGE_QUERY
UNSUPPORTED
```

---

## 9. Mock 策略

一期目标：

> 流程真实，结果可以 Mock。

Mock 必须：

1. 参数感知；
2. 模型感知；
3. Execution 感知；
4. 输出符合统一 Contract；
5. 能被 E2E 测试验证。

例如：

```text
EXEC_001
POR = default
MODEL = A

EXEC_002
POR = 0.16
PERM = 0.16
MODEL = A

EXEC_003
POR = 0.16
PERM = 0.16
MODEL = B
```

---

## 10. 异步任务与事件

目标单井解释可能超过 2 分钟，因此后续 Task 08 增加跨请求后台 Execution 生命周期：

```text
Agent
   ↓
TaskService
   ↓
创建 Execution
   ↓
Queue
   ↓
立即返回 execution_id
   ↓
Worker 执行 Workflow
```

AgentScope Web 当前已有 `/sessions/{session_id}/stream` 与流式消息，且能显示 W01～W10 的请求期进度。一期优先把 Execution、Stage、Tool 状态投影到现有 AgentScope SSE/Stream，不预设第二套 SSE。后台执行的持久状态、断线查询/回放由后续 Task 08 处理。目标事件：

```text
INTENT_RECOGNIZED
COMMAND_ACCEPTED
EXECUTION_CREATED
STAGE_REUSED
STAGE_STARTED
STAGE_COMPLETED
STAGE_FAILED
TOOL_STARTED
TOOL_COMPLETED
TOOL_FAILED
REPORT_READY
EXECUTION_COMPLETED
EXECUTION_FAILED
```

---

## 11. 核心 E2E 验收场景

### CASE-01 首次解释

用户：

```text
帮我解释一下这口井。
```

预期：

```text
DATA_DECODE   RUN
PREPROCESS    RUN
INTERPRET     RUN
REPORT        RUN
```

产物：

```text
EXEC_001
INPUT_V001
RESULT_V001
REPORT_001
```

### CASE-02 修改采样间隔

用户：

```text
采样间隔改成0.1重新解释。
```

预期：

```text
DATA_DECODE   REUSED
PREPROCESS    RUN
INTERPRET     RUN
REPORT        RUN
```

### CASE-03 更换预测模型

用户：

```text
换成模型B重新解释。
```

预期：

```text
DATA_DECODE   REUSED
PREPROCESS    REUSED
INTERPRET     RUN
REPORT        RUN
```

### CASE-04 修改 POR/PERM

用户：

```text
把孔隙度、渗透率改成0.16，重新解释一下。
```

预期：

```text
intent = MODIFY_AND_RERUN

Override:
POR  = 0.16
PERM = 0.16

DATA_DECODE   REUSED
PREPROCESS    REUSED
INTERPRET     RUN
REPORT        RUN
```

必须验证：

```text
新建 Execution
Override 与新 Execution 绑定
Mock Prediction 收到 POR=0.16 / PERM=0.16
生成新的 ReportArtifact
历史 Execution/Report 保留
```

### CASE-05 全量重跑

用户：

```text
之前结果不用了，全部重新跑一次。
```

预期四阶段全部 RUN。

### CASE-06 查询状态

智能处理期间用户：

```text
现在处理到哪里了？
```

预期 AgentScope 交互层调用 `get_task_status`，从业务 Repository 读取真实状态后回答，禁止根据聊天上下文猜测。

---

## 12. Codex 实施顺序

实施编号、文件边界、验收和风险以 `docs/06-interactive-agent-gap-analysis.md` 第 8 节为准，不沿用本设计文档原先假定的“新建四阶段 WorkflowRunner”顺序：

| Task | 渐进实施主题 |
|---|---|
| 01 | 扩展现有 Task 持久化并新增版本化 Execution，保持现有执行入口不变 |
| 02 | 可恢复的输入版本与 InterpretationOverride 契约 |
| 03 | DependencyResolver 与 ExecutionPlan |
| 04 | 复用状态装配及现有 InterpretationWorkflow.run(...) 的受控局部重跑 |
| 05 | 参数感知 Mock 与预处理输入版本验证 |
| 06 | Execution 报告绑定与 ToolRun 记录 |
| 07 | 任务级命令及现有 AgentScope 交互外壳升级 |
| 08 | 后台 Execution、真实状态与现有 Stream 事件复用 |
| 09 | 增量 Web 展示及 CASE-01～CASE-06 回归验收 |
| 10 | 真实 Heavy Prediction API 适配，协议确认后实施 |
| 11 | 真实 Report API 适配，协议确认后实施 |

---

## 13. 一期明确暂不做

- 通用 DAG/BPM 平台；
- 多 Agent 协作；
- LLM 自主规划测井专业流程；
- Heavy API 计算过程断点续算；
- 复杂人工回滚；
- 精细百分比进度；
- 所有 Logical Tool 的真实原子算法；
- Reflexion；
- 通用工作流编辑器。

---

## 14. 当前待外部确认项

以下内容不阻塞 Codex 开工，但接真实 API 前必须补齐：

1. Heavy API 输入文件格式；
2. POR/PERM 等 Override 在输入文件中的具体写入方式；
3. 三种预测模型的选择方式；
4. Heavy API 返回文件格式；
5. 结果文件中岩性、物性、流体、层类型、层段字段映射；
6. Report API 输入协议；
7. Report API 返回报告路径/文件的方式；
8. 外部 API 是否支持 job_id、状态查询、cancel。

本文定位为**详细契约、交互规则与 E2E 目标**；`docs/04-interactive-agent-architecture.md` 描述目标架构与总体原则，`docs/06-interactive-agent-gap-analysis.md` 记录实际代码现状、差距和实施顺序。当前是否已实现某能力，以 06 的代码扫描事实为准。
