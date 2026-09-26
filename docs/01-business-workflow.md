# 测井解释业务 Workflow 设计

## 1. 文档目的

本文档定义常规测井解释智能体的核心业务 Workflow。

目标是将测井解释工程师理解的业务流程转换为程序可执行的系统流程，使系统能够：

- 明确每一步输入；
- 明确每一步输出；
- 检查前置条件；
- 记录状态变化；
- 处理数据缺失；
- 处理执行失败；
- 支持重试；
- 支持回退；
- 支持人工复核；
- 保留执行轨迹。

本文档只定义业务 Workflow。

暂不定义：

- 具体 AgentScope API；
- 具体 Agent 实现；
- 具体 Tool 实现；
- 具体数据库表结构；
- 具体专业算法公式。

## 2. 业务主流程

测井解释核心业务流程为：

```text
原始资料准备
↓
数据预处理与质量控制
↓
岩性识别
↓
储层识别与物性评价
↓
流体识别
↓
油气水层分类
↓
层段划分与有效厚度计算
↓
岩心 / 录井 / 试油 / 邻井综合验证
↓
形成单井测井解释报告
```

为了适配智能体系统，进一步转换为：

```text
W01 任务初始化与原始资料加载
↓
W02 数据完整性检查
↓
W03 数据预处理与质量控制
↓
W04 岩性识别
↓
W05 储层识别与物性评价
↓
W06 流体识别
↓
W07 油气水层分类
↓
W08 层段划分与有效厚度计算
↓
W09 多源资料综合验证
↓
W10 最终一致性检查与报告生成
```

## 3. Workflow 总体原则

### 3.1 Workflow 负责流程控制

Workflow 负责：

- 步骤顺序；
- 前置条件；
- 条件分支；
- 重试；
- 回退；
- 人工复核；
- 状态变更。

Workflow 不负责替代专业推理和专业算法。

### 3.2 所有步骤围绕统一状态运行

Workflow 统一使用：

```text
InterpretationState
```

每一个节点执行模式为：

```text
读取 InterpretationState
↓
检查前置条件
↓
执行业务步骤
↓
生成结果
↓
更新 InterpretationState
↓
判断下一节点
```

### 3.3 每个节点统一状态

每个 Workflow 节点至少具有以下状态：

```text
PENDING
RUNNING
SUCCESS
WARNING
FAILED
BLOCKED
REVIEW_REQUIRED
SKIPPED
```

含义：

- **PENDING**：等待执行。
- **RUNNING**：正在执行。
- **SUCCESS**：步骤正常完成。
- **WARNING**：步骤完成，但存在非阻断问题。
- **FAILED**：执行失败。
- **BLOCKED**：因为关键数据或前置条件缺失，当前步骤无法执行。
- **REVIEW_REQUIRED**：需要人工确认。
- **SKIPPED**：经过明确流程规则允许跳过。

## 4. 数据完整性等级

所有业务数据分为三级。

### 4.1 Required

缺失后对应业务步骤无法正常执行。

### 4.2 Recommended

缺失不会阻断步骤，但可能降低解释可靠性或证据充分性，应产生 Warning。

### 4.3 Optional

用于增强解释、辅助验证和提高结果可信度，缺失原则上不影响主流程。

## 5. W01：任务初始化与原始资料加载

### 5.1 业务目标

创建一次单井测井解释任务，并加载后续解释需要的基础数据。

### 5.2 前置条件

至少需要：

```text
task_id
well_id
```

可选：

```text
depth_from
depth_to
target_interval
```

### 5.3 输入

主要输入：

```text
TaskRequest
```

示意：

```json
{
  "task_id": "TASK_001",
  "well_id": "WELL_001",
  "depth_from": 2000,
  "depth_to": 3000
}
```

### 5.4 核心处理

1. 创建解释任务；
2. 获取井基本信息；
3. 获取原始测井数据；
4. 获取地质资料；
5. 获取录井资料；
6. 获取岩心资料；
7. 获取试油资料；
8. 获取邻井资料；
9. 初始化 InterpretationState。

当前第一阶段允许部分数据为 Mock。

### 5.5 输出

```text
WellRawData
InterpretationState
```

### 5.6 State 更新

```text
state.task
state.well
state.raw_data
state.current_step = W01
```

### 5.7 成功条件

任务创建成功，并能够建立 InterpretationState。即使部分非关键数据不存在，也允许 W01 成功。

### 5.8 失败条件

例如：

- well_id 无效；
- 无法获取任何井基础资料；
- 数据服务完全不可访问；
- InterpretationState 初始化失败。

### 5.9 重试

外部数据读取失败时允许重试。

### 5.10 下一节点

```text
W02
```

## 6. W02：数据完整性检查

### 6.1 业务目标

判断当前数据是否满足后续各业务节点执行条件。

### 6.2 输入

```text
state.well
state.raw_data
```

### 6.3 核心处理

检查：

- 基础井信息；
- 测井曲线；
- 地质资料；
- 岩心；
- 录井；
- 试油；
- 邻井资料。

重点不是“数据是否全部存在”，而是：

```text
缺少什么数据
↓
影响哪个步骤
↓
是否阻断流程
```

### 6.4 输出

```text
DataCompletenessResult
```

建议表达：

```json
{
  "required_missing": [],
  "recommended_missing": [],
  "optional_missing": [],
  "affected_steps": [],
  "status": "PASS"
}
```

### 6.5 State 更新

```text
state.missing_data
state.warnings
state.current_step = W02
```

### 6.6 状态判断

- **PASS**：关键数据完整。
- **PASS_WITH_WARNING**：存在 Recommended / Optional 数据缺失。
- **BLOCKED**：后续关键业务能力所需 Required 数据缺失。

### 6.7 BLOCKED 处理

如果只影响部分后续步骤，不一定立即终止整个任务。系统应记录：

```text
missing_data
affected_step
```

并在执行对应步骤时再次判断。

### 6.8 下一节点

正常情况下：

```text
W03
```

## 7. W03：数据预处理与质量控制

### 7.1 业务目标

对原始测井数据进行预处理，并检查数据质量。

### 7.2 输入

```text
state.raw_data
state.well
```

### 7.3 核心处理

候选处理包括：

- 数据范围检查；
- 空值检查；
- 异常值检查；
- 深度匹配；
- 曲线对齐；
- 井径影响分析；
- 环境校正；
- 曲线质量评价。

具体算法由专业 Tool 承担。

### 7.4 输出

```text
ProcessedLogData
QCResult
```

### 7.5 State 更新

```text
state.processed_data
state.qc_result
state.warnings
state.current_step = W03
```

### 7.6 成功条件

关键测井数据能够进入后续解释。

### 7.7 Warning 条件

例如部分井段存在井径扩大、曲线质量较差或局部缺失，但整体仍可解释。此时状态为 WARNING，并记录受影响深度范围。

### 7.8 BLOCKED 条件

如果关键测井数据质量严重异常，以至于无法支持后续解释，进入 BLOCKED，可进入人工复核。

### 7.9 下一节点

```text
W04
```

## 8. W04：岩性识别

### 8.1 业务目标

根据处理后的测井数据和相关地质信息识别井段岩性。

### 8.2 输入

可能包括：

```text
GR
SP
DEN
CNL
AC
PE
地质资料
state.qc_result
```

具体使用字段由后续算法和 Tool Contract 定义。

### 8.3 前置条件

需要满足：

- 对应曲线存在；
- QC 结果允许使用。

### 8.4 核心处理

```text
测井特征提取
↓
必要专业参数计算
↓
岩性候选识别
↓
岩性综合判断
```

### 8.5 输出

```text
LithologyResult
```

至少应包含：

```text
depth / interval
lithology
evidence
warnings
```

### 8.6 State 更新

```text
state.lithology_result
state.current_step = W04
```

### 8.7 失败处理

如果数据不足，根据缺失数据重要程度进入 BLOCKED 或 WARNING。

### 8.8 下一节点

```text
W05
```

## 9. W05：储层识别与物性评价

### 9.1 业务目标

计算储层相关参数，并对储层及物性进行评价。

### 9.2 输入

```text
state.processed_data
state.lithology_result
state.qc_result
```

### 9.3 推荐子 Workflow

```text
W05-01 泥质含量计算
↓
W05-02 孔隙度计算
↓
W05-03 渗透率计算
↓
W05-04 储层识别
↓
W05-05 储层物性评价
```

### 9.4 W05-01：泥质含量计算

输入候选：

```text
GR
其他必要参数
```

输出：

```text
Vsh
```

结果写入 `state.petrophysics_result`。

### 9.5 W05-02：孔隙度计算

输入候选：

```text
DEN
CNL
AC
LithologyResult
```

输出：

```text
Porosity
```

### 9.6 W05-03：渗透率计算

输入候选：

```text
Porosity
Vsh
经验模型参数
```

输出：

```text
Permeability
```

### 9.7 W05-04：储层识别

综合：

```text
Lithology
Vsh
Porosity
Permeability
```

判断目标井段是否具有储层特征。

输出：

```text
ReservoirResult
```

### 9.8 W05-05：储层物性评价

形成：

```text
PetrophysicsResult
```

### 9.9 W05 State 更新

```text
state.petrophysics_result
state.current_step = W05
```

### 9.10 W05 失败处理

某个专业计算 Tool 失败时，首先按照 Tool 策略重试。重试失败后记录 ToolError，并判断：

- 是否可使用替代算法；
- 是否允许降级；
- 是否 Block；
- 是否人工复核。

不得由 LLM 自行补造计算结果。

### 9.11 下一节点

```text
W06
```

## 10. W06：流体识别

### 10.1 业务目标

综合储层、物性、电阻率及相关资料，对目标井段流体性质进行识别。

### 10.2 输入

可能包括：

```text
LithologyResult
PetrophysicsResult
RT
RXO
深浅电阻率
Sw
侵入特征
录井资料
```

### 10.3 内部处理

```text
Sw 计算
↓
电阻率特征分析
↓
侵入特征分析
↓
多参数综合判断
↓
流体识别
```

其中确定性计算必须通过 Tool 执行。

### 10.4 输出

```text
FluidResult
```

应至少能够表达：

```text
fluid_type
evidence
conflicts
missing_evidence
warnings
```

### 10.5 State 更新

```text
state.fluid_result
state.current_step = W06
```

### 10.6 失败 / 不确定处理

当证据不足时，不得强制形成确定流体结论。应允许 REVIEW_REQUIRED 或输出 UNCERTAIN。

### 10.7 下一节点

```text
W07
```

## 11. W07：油气水层分类

### 11.1 业务目标

综合前面阶段结果，对井段进行最终解释层类型分类。

### 11.2 输入

主要包括：

```text
LithologyResult
PetrophysicsResult
FluidResult
测井参数
录井信息
```

### 11.3 核心处理

综合多个证据形成层类型判断。

候选结果例如：

```text
油层
气层
水层
油水同层
气水同层
差油层
干层
非储层
```

实际分类体系以后以项目业务标准为准。

### 11.4 输出

```text
LayerClassificationResult
```

### 11.5 State 更新

```text
state.layer_classification
state.current_step = W07
```

### 11.6 规则

禁止单一参数直接决定复杂层类型，分类必须保留主要依据。

### 11.7 下一节点

```text
W08
```

## 12. W08：层段划分与有效厚度计算

### 12.1 业务目标

将连续深度解释结果组织成正式解释层段，并计算厚度。

### 12.2 输入

```text
state.layer_classification
state.petrophysics_result
```

### 12.3 核心处理

包括：

- 连续层段合并；
- 层界识别；
- 夹层处理；
- 总厚度计算；
- 有效厚度计算。

### 12.4 输出

```text
IntervalResult
```

例如：

```text
top_depth
bottom_depth
layer_type
gross_thickness
effective_thickness
```

### 12.5 State 更新

```text
state.interval_result
state.current_step = W08
```

### 12.6 下一节点

```text
W09
```

## 13. W09：多源资料综合验证

### 13.1 业务目标

利用独立资料对前面的测井解释结果进行综合验证。

### 13.2 输入

包括：

```text
LayerClassificationResult
IntervalResult
岩心资料
录井资料
试油资料
邻井资料
地质资料
```

### 13.3 核心处理

对比：

```text
测井解释结果
VS
实际或辅助证据
```

判断：

- `CONSISTENT`（证据一致）：多源验证证据与当前解释总体一致。
- `PARTIAL_CONFLICT`（部分冲突）：存在局部不一致，但不足以认定整体解释失效。
- `SERIOUS_CONFLICT`（严重冲突）：关键验证证据与当前解释存在明显冲突。
- `INSUFFICIENT_EVIDENCE`（证据不足）：独立验证证据数量、覆盖范围或可信度不足，无法确认当前解释是否可靠；**不等于当前解释一定错误**。

完整说明见 [11-status-enum-glossary.md](11-status-enum-glossary.md)。

### 13.4 输出

```text
ValidationResult
```

至少包含：

```text
validation_status
supporting_evidence
conflicting_evidence
affected_intervals
recommended_action
```

### 13.5 State 更新

```text
state.validation_result
state.current_step = W09
```

## 14. W09 分支规则

### 14.1 一致

`CONSISTENT`（证据一致）→ W10。

### 14.2 轻微冲突

`PARTIAL_CONFLICT`（部分冲突）→ 当前实现记录 `WARNING`（完成但有告警）并继续后续检查。

### 14.3 严重冲突

`SERIOUS_CONFLICT`（严重冲突）表示已有关键证据与当前解释明显矛盾，不能继续把当前结果当作已验证结论。

当前实现进入 `REVIEW_REQUIRED`（需要人工复核），不会自动回退 W06/W07。自动定位冲突环节并回退重算属于后续能力。

### 14.4 证据不足

`INSUFFICIENT_EVIDENCE`（证据不足）表示可用于验证的独立证据不足，系统无法确认当前解释是否可靠。它和“严重冲突”不同：这里不是已有证据明确反对当前结论，而是证据不够。

当前实现同样进入 `REVIEW_REQUIRED`（需要人工复核）。

## 15. 回退机制

长期目标允许根据验证结果进行受控回退，例如：

```text
W09 → W06
W09 → W07
W10 → W04
W10 → W05
W10 → W06
W10 → W07
```

实际可回退节点根据发现的问题决定。**当前实现尚未自动执行上述回退**；W09 的 `SERIOUS_CONFLICT`（严重冲突）和 `INSUFFICIENT_EVIDENCE`（证据不足）先进入 `REVIEW_REQUIRED`（需要人工复核）。

### 15.1 回退记录

每次回退必须记录：

```text
rollback_from
rollback_to
reason
trigger_source
timestamp
retry_count
```

### 15.2 最大回退次数

禁止无限循环。每个任务应设置：

```text
max_retry_count
max_rollback_count
```

超过限制后进入 REVIEW_REQUIRED，由人工复核。具体次数后续配置。

## 16. W10：最终一致性检查

### 16.1 业务目标

在正式生成报告之前，检查整个解释任务是否完整、自洽。

### 16.2 检查内容

至少检查：

- 所有目标井段是否完成解释；
- 是否存在未处理 Failed 节点；
- 是否存在 Blocked 节点；
- 是否存在关键 Missing Data；
- 是否存在严重 Validation Conflict；
- 是否存在失败 Tool；
- 是否存在未处理人工复核事项；
- 阶段结果之间是否存在明显矛盾。

### 16.3 输出

```text
FinalValidationResult
```

可能为：

```text
PASS
PASS_WITH_WARNING
REVIEW_REQUIRED
FAILED
```

## 17. 报告生成

Final Validation 通过后：

```text
InterpretationState
↓
结构化结果整理
↓
JSON
↓
Markdown Report
```

### 17.1 JSON 输出

JSON 用于：

- 系统持久化；
- 自动测试；
- 后续接口；
- 报告生成；
- 结果分析。

### 17.2 Markdown 输出

Markdown 面向：

- 测井解释工程师；
- 项目演示；
- 人工复核。

第一阶段暂不要求 Word / PDF。

## 18. 人工复核机制

以下情况建议进入 REVIEW_REQUIRED：

- Required 数据缺失；
- QC 严重异常；
- 关键专业 Tool 多次失败；
- 流体识别证据严重不足；
- 多源资料严重冲突；
- Workflow 多次回退；
- 最终结果无法自洽。

人工复核完成后应支持：

```text
继续执行
重新执行某节点
人工确认结果
终止任务
```

## 19. Workflow 执行记录

每一个节点执行时至少记录：

```text
step_id
status
start_time
end_time
input_summary
output_summary
warnings
errors
tool_calls
agent_calls
retry_count
rollback_history
```

用于：

- Trace；
- Debug；
- Eval；
- 结果复盘。

## 20. Workflow 与 InterpretationState 的关系

```text
InterpretationState
        │
        ↓
       W01
        │
更新 State
        ↓
       W02
        │
更新 State
        ↓
       W03
        │
       ...
        ↓
       W10
        │
        ↓
Final InterpretationState
```

任何 Agent 和 Tool 都不得绕过 Workflow 随意改变核心业务状态。核心状态修改应具备明确来源。

## 21. 第一阶段 Mock 策略

V0.1 允许部分节点使用 Mock。

例如：

```text
Real Workflow
+
Mock Well Data
+
Mock QC Tool
+
Mock Porosity Tool
+
Mock Sw Tool
+
Real Agent Invocation
+
Real State Transition
+
Real Validation Branch
+
Real Report Generation
```

第一阶段重点验证整个 Workflow 是否真正可以跑通，而不是验证所有专业算法是否已经达到生产级准确率。

## 22. 第一阶段必须验证的异常场景

### Case 01：正常井

所有必要数据存在。

预期：

```text
W01 → W10 → Report
```

### Case 02：缺少 Recommended 数据

预期 WARNING，但 Workflow 可以继续。

### Case 03：缺少 Required 数据

预期对应步骤 BLOCKED，不得生成虚假专业结果。

### Case 04：专业 Tool 执行失败

预期：

```text
Tool Failed
→ Retry
→ 仍失败
→ Error / Review
```

### Case 05：验证发现严重冲突

当前实现预期：

```text
W09
→ SERIOUS_CONFLICT（严重冲突）
→ REVIEW_REQUIRED（需要人工复核）
```

自动 Rollback（回退重算）是后续能力，不在当前实现中冒充已完成。

### Case 06：达到最大回退次数

预期 REVIEW_REQUIRED，停止自动循环。

## 23. 当前暂未确定事项

以下内容后续确定：

1. 每个节点正式 Input Schema；
2. 每个节点正式 Output Schema；
3. Required / Recommended / Optional 完整字段表；
4. 专业算法实现；
5. 各类解释阈值；
6. max_retry_count；
7. max_rollback_count；
8. 人工复核 Web 交互形式；
9. AgentScope 中 Workflow 的具体实现方式；
10. Workflow State 与 Redis / DB 的持久化边界。

Codex 不应在没有设计依据的情况下自行确定这些专业规则。

## 24. 文档定位

本文档是：

> 常规测井解释智能体的业务 Workflow 规范。

后续 Agent 设计、Tool 设计、State 设计、Codex 实现、Workflow 测试和 Evaluation 均应遵循本文档定义的业务顺序、状态流转和异常处理原则。
