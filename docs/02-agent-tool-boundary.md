# Agent / Workflow / Tool 职责边界设计

状态、枚举和流程节点的中文含义统一见 [11-status-enum-glossary.md](11-status-enum-glossary.md)。本文保留英文代码值用于和实现、日志及测试对应。

## 1. 文档目的

本文档定义测井解释智能体中以下能力的职责边界：

- Agent；
- Workflow；
- Tool；
- Python Algorithm；
- 普通 LLM 调用。

目标是避免：

- 为了多 Agent 而过度拆分；
- 使用 LLM 替代确定性算法；
- Workflow、Agent、Tool 职责混乱；
- Agent 之间产生大量不必要的自然语言交互；
- 系统随着业务扩展逐渐失控。

V0.1 优先追求：

> 简单、稳定、低耦合、可测试、可替换。

## 2. 核心判断原则

### 2.1 Python Algorithm

如果任务满足：

```text
明确输入
↓
固定计算公式 / 算法
↓
确定输出
```

优先实现为 Python Algorithm。

例如：

- Vsh 计算；
- 孔隙度计算；
- 渗透率计算；
- Sw 计算；
- 有效厚度计算；
- 深度匹配。

Algorithm 原则上不关心 Agent、Workflow、数据库、Redis、Web，应尽可能保持纯业务计算能力。

## 3. Tool

如果某个能力需要被 Agent 或 Workflow 调用，并具有明确输入、明确职责和明确输出，则封装为 Tool。

Tool 内部可以调用：

- Python Algorithm；
- Repository；
- External API；
- 数据服务；
- RAG；
- 其他基础设施 Adapter。

例如：

```text
calculate_porosity
get_log_data
get_core_data
calculate_sw
merge_intervals
```

Tool 是 Agent 与实际业务执行能力之间的重要边界。

## 4. Workflow

Workflow 适用于：

```text
已知业务顺序
+
明确条件判断
+
确定性步骤编排
```

例如：

```text
加载数据
↓
完整性检查
↓
QC
↓
岩性
↓
储层评价
```

Workflow 负责步骤顺序、条件分支、状态流转、Retry、Rollback、Review 和 Error Handling。Workflow 不应该负责复杂专业推理。

## 5. 普通 LLM 调用

如果任务只是：

```text
已有结构化结果
↓
理解 / 总结 / 表述
↓
生成文本
```

通常不需要建立 Agent。

例如：

```text
QCResult
↓
LLM
↓
生成 QC 摘要
```

或者：

```text
InterpretationState
↓
LLM
↓
生成 Markdown 报告
```

这类能力优先采用普通模型调用。

## 6. Agent

只有当任务需要：

```text
观察当前状态
↓
分析当前信息
↓
自主决定下一步
↓
选择 Tool
↓
获取新的结果
↓
继续推理
↓
动态决定下一步
```

才建立 Agent。

Agent 的核心价值是：

> 决策，而不是计算。

## 7. V0.1 总体职责划分

第一阶段采用：

```text
MainAgent
InterpretationAgent
ValidationAgent
```

三个核心 Agent。

其他大量业务能力由 Workflow + Tool + Python Algorithm + 普通 LLM 承担。

## 8. W01～W10 实现边界

### W01 任务初始化与资料加载

推荐：

```text
Workflow
+
Repository Tool
+
Data Tool
```

不建立 Agent。

典型 Tool：

```text
get_well_info
get_log_data
get_geology_data
get_core_data
get_mud_logging_data
get_test_data
get_offset_well_data
```

### W02 数据完整性检查

推荐：

```text
Workflow
+
Rule Engine
```

必要时封装为 `DataCompletenessTool`。

不建立 Agent。

### W03 数据预处理与质量控制

推荐：

```text
Workflow
+
Python Algorithm
+
QC Tool
```

不建立 QC Agent。

典型能力：

```text
depth_match
curve_alignment
detect_missing_value
detect_outlier
borehole_quality_check
curve_quality_check
```

### W04 岩性识别

V0.1 推荐：

```text
Workflow
+
专业 Tool
+
必要 LLM 推理
```

暂时不建立 LithologyAgent。

内部可以包含：

```text
calculate_vsh
↓
提取测井响应特征
↓
lithology_classifier
↓
必要的综合判断
```

输出：

```text
LithologyResult
```

如果未来岩性识别需要动态选择多种解释模型、主动查询地层资料、多轮调用不同工具或对复杂岩性持续推理，再升级为独立 Agent。

### W05 储层识别与物性评价

推荐：

```text
Sub Workflow
+
Python Algorithm
+
Tool
+
必要的 LLM / Rule
```

暂时不建立 ReservoirAgent。

建议内部结构：

```text
calculate_vsh
calculate_porosity
calculate_permeability
reservoir_identification
property_evaluation
```

前三类计算必须优先采用专业算法。

### W06 流体识别

推荐：

```text
InterpretationAgent
+
专业 Tool
```

这是 V0.1 第一个明确需要专业 Agent 推理的核心业务步骤。

流体识别可能需要综合：

```text
Lithology
Porosity
Permeability
Sw
RT
RXO
深浅电阻率
侵入特征
录井信息
其他辅助证据
```

InterpretationAgent 需要能够决定：

```text
是否需要补充数据
是否调用新的 Tool
是否重新计算某项参数
现有证据是否足够
是否应该输出不确定结果
```

### W07 油气水层分类

V0.1 继续由 InterpretationAgent 负责，不单独建立 LayerClassificationAgent。

W06 与 W07 共享大量上下文，因此 V0.1 采用：

```text
InterpretationAgent

内部步骤：

Fluid Identification
↓
Layer Classification
```

未来当流体识别业务逻辑非常复杂、需要大量独立工具、需要不同模型、需要独立评估或 Context 明显过大时，再考虑拆分。

### W08 层段划分与有效厚度

推荐：

```text
Workflow
+
Python Algorithm
+
Tool
```

不建立 IntervalAgent。

主要能力：

```text
merge_intervals
detect_layer_boundary
calculate_gross_thickness
calculate_effective_thickness
```

### W09 多源资料综合验证

推荐：

```text
ValidationAgent
+
Data Tools
```

ValidationAgent 综合：

```text
测井解释结果
岩心
录井
试油
邻井
地质资料
```

判断：

- `CONSISTENT`（证据一致）
- `PARTIAL_CONFLICT`（部分冲突）
- `SERIOUS_CONFLICT`（严重冲突）
- `INSUFFICIENT_EVIDENCE`（证据不足：现有独立证据不足以验证当前解释，并不等于解释结论一定错误）

### W10 最终一致性检查

推荐：

```text
Workflow
+
Rule / Validation Tool
```

不需要独立 FinalCheckAgent。

报告生成 V0.1 推荐：

```text
普通 LLM 调用
+
Report Template
```

暂时不建立 ReportAgent。

## 9. ValidationAgent 不直接修改解释结果

重要原则：

```text
InterpretationAgent
↓
生成解释结果

ValidationAgent
↓
验证解释结果
```

ValidationAgent 不应该直接修改 LayerClassificationResult，而应输出 ValidationResult，包括：

```text
冲突位置
冲突类型
证据
建议回退节点
是否建议人工复核
```

最终由 Workflow / MainAgent 决定继续、Rollback、Retry 或 Review。

## 10. MainAgent

MainAgent 定位为：

```text
Planner
+
Orchestrator
```

主要职责：

1. 理解用户请求；
2. 确认解释目标；
3. 创建或启动解释任务；
4. 启动业务 Workflow；
5. 查看 InterpretationState；
6. 调度专业 Agent；
7. 处理 Agent 执行结果；
8. 处理异常；
9. 处理回退；
10. 处理人工复核状态；
11. 汇总最终结果。

MainAgent 不直接计算孔隙度、渗透率、Sw，不修改原始测井数据，不绕过 Workflow 修改最终状态。

## 11. MainAgent 与 Workflow 的关系

推荐：

```text
用户
↓
MainAgent
↓
Interpretation Workflow
↓
W01-W10
```

当 Workflow 到达需要专业推理的位置：

```text
Workflow
↓
InterpretationAgent / ValidationAgent
```

MainAgent 不应自己手动执行 W01-W10 的每一个细节。

## 12. InterpretationAgent

V0.1 主要负责：

```text
W06 流体识别
+
W07 油气水层分类
```

可以读取：

```text
LithologyResult
PetrophysicsResult
ProcessedLogData
QCResult
相关录井资料
当前 Interval 信息
已有 Warning / Missing Data
```

候选可调用 Tool：

```text
calculate_sw
analyze_resistivity
analyze_invasion
get_mud_logging_data
get_core_data
get_test_data
get_offset_well_data
```

输出必须结构化，例如：

```text
FluidResult
LayerClassificationResult
Evidence
Conflict
MissingEvidence
RecommendedAction
```

## 13. ValidationAgent

负责独立检查解释结果，重点不是重新做一遍 InterpretationAgent，而是使用独立证据验证解释是否合理。

可以读取：

```text
Interpretation Result
Interval Result
Core Data
Mud Logging
Well Test
Offset Well
Geology
```

输出：

```text
ValidationResult
```

必须结构化描述：

```text
status
supporting_evidence
conflicting_evidence
affected_interval
recommended_action
rollback_target
review_required
```

## 14. Agent 之间的关系

V0.1 不推荐 Agent 随意直接互相聊天。

推荐：

```text
InterpretationAgent
↓
Structured Result
↓
InterpretationState

ValidationAgent
↓
读取 State
↓
Structured ValidationResult
```

Agent 之间通过 InterpretationState + Workflow 协作，而不是依赖大量自然语言通信。

## 15. Agent 与 Tool 的关系

推荐：

```text
Agent
↓
Tool Interface
↓
Tool Implementation
```

Agent 不直接依赖数据库 SDK、Redis SDK、HTTP Client 或具体算法类，通过 Tool / Adapter 隔离。

## 16. Tool 分类

### 16.1 Data Tools

```text
get_well_info
get_log_data
get_core_data
get_mud_logging_data
get_test_data
get_offset_well_data
```

### 16.2 QC Tools

```text
check_curve_quality
depth_match
detect_anomaly
```

### 16.3 Petrophysics Tools

```text
calculate_vsh
calculate_porosity
calculate_permeability
calculate_sw
```

### 16.4 Interpretation Support Tools

```text
analyze_resistivity
analyze_invasion
extract_interval_features
```

### 16.5 Interval Tools

```text
merge_intervals
calculate_gross_thickness
calculate_effective_thickness
```

## 17. Tool 输出原则

Tool 应返回结构化结果，而不是只返回裸数值。

示例：

```json
{
  "value": 31.2,
  "unit": "ohm.m",
  "status": "SUCCESS",
  "source": "RT",
  "warnings": []
}
```

便于 Trace、Error Handling、Evaluation、Agent 理解和后续算法替换。

## 18. Mock Tool 与 Real Tool

第一阶段允许 Mock Tool，但 Mock 与 Real 应遵循同一个 Contract：

```text
PorosityTool interface
├── MockPorosityTool
└── RealPorosityTool
```

上层 Workflow 和 Agent 不需要因为 Tool 实现变化而大规模修改。

## 19. LLM 调用边界

LLM 适合：

```text
综合证据
解释复杂信息
生成阶段性文字说明
形成报告
理解用户任务
```

LLM 不适合替代：

```text
确定性公式
原始数据读取
数据库查询逻辑
状态机
厚度计算
明确规则检查
```

## 20. W01～W10 最终职责矩阵

| 节点 | Workflow | Tool / Algorithm | LLM | Agent |
|---|---|---|---|---|
| W01 数据加载 | ✓ | ✓ |  |  |
| W02 完整性检查 | ✓ | ✓ | 可选 |  |
| W03 QC | ✓ | ✓ | 可选 |  |
| W04 岩性识别 | ✓ | ✓ | ✓ | 暂不需要 |
| W05 储层物性 | ✓ | ✓ | 可选 | 暂不需要 |
| W06 流体识别 | ✓ | ✓ | ✓ | InterpretationAgent |
| W07 层分类 | ✓ | ✓ | ✓ | InterpretationAgent |
| W08 层段/厚度 | ✓ | ✓ |  |  |
| W09 综合验证 | ✓ | ✓ | ✓ | ValidationAgent |
| W10 最终检查 | ✓ | ✓ | 报告生成使用 |  |

## 21. V0.1 Agent 数量

当前正式基线：

```text
3 个 Agent
```

分别为：

```text
MainAgent
InterpretationAgent
ValidationAgent
```

不以 Agent 数量体现系统复杂度。

## 22. 未来可扩展 Agent

未来根据业务复杂度，可以考虑：

```text
LithologyAgent
ReservoirAgent
FluidAgent
LayerClassificationAgent
ReportAgent
```

但必须满足：

> 独立 Agent 带来的收益明显大于增加的通信、状态、模型调用和维护成本。

## 23. 不允许出现的设计

禁止：

- 每个业务步骤一个 Agent；
- 所有计算全部交给 LLM；
- Agent 直接操作数据库并自行修改业务状态；
- 多个 Agent 依赖长自然语言互相传递核心业务数据；
- Tool 同时负责业务决策和专业计算；
- MainAgent 承担所有专业解释任务。

## 24. 第一阶段重点

V0.1 优先验证：

```text
MainAgent
↓
Workflow
↓
Tool
↓
InterpretationAgent
↓
Workflow
↓
ValidationAgent
↓
Workflow
↓
Report
```

证明这条链路真正可运行后，再根据实际效果拆分更多专业 Agent。

## 25. 当前开放问题

后续需要继续设计：

1. AgentScope 中 MainAgent 与 Workflow 的具体实现关系；
2. InterpretationAgent Tool 权限；
3. ValidationAgent Tool 权限；
4. Agent Context Builder；
5. Agent System Prompt；
6. Tool Contract Schema；
7. Agent 输出 Schema；
8. Agent 失败和超时机制；
9. 模型调用失败策略；
10. MainAgent 是否允许直接调用专业 Tool；
11. 未来是否拆分 FluidAgent；
12. 岩性识别最终采用什么算法组合。

以上事项不得由 Codex 无依据自行决定。

## 26. 文档定位

本文档是测井解释智能体 Agent / Workflow / Tool / LLM 的职责边界基线。

后续总体技术架构、Agent Prompt、Tool Design、Codex 实现均应遵循本文档。

如果后续需要增加新的 Agent，应先说明：

- 为什么现有 Workflow / Tool 无法满足；
- 新 Agent 的独立目标是什么；
- 它需要哪些 Tool；
- 它与已有 Agent 如何隔离；
- 增加 Agent 后带来的收益是什么。
