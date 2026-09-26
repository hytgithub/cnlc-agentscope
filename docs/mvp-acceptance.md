# 测井解释 Agent V0.1 MVP 验收标准

状态、枚举和验证结论的中文定义见 [11-status-enum-glossary.md](11-status-enum-glossary.md)。

## 1. 文档目的

本文档定义测井解释 Agent 第一阶段 V0.1 的验收标准。

V0.1 的核心目标不是立即达到生产级测井解释准确率，也不是完成全部真实专业算法。

V0.1 主要验证：

> 当前设计的 Agent + Workflow + Tool + State + DB + Redis + Trace + Web + Report 架构是否可以真实运行。

第一阶段最重要的成功标准为：

> **整条主链路跑通，并且能够稳定重复执行。**

## 2. MVP 总体目标

V0.1 至少应证明：

```text
用户提交解释任务
↓
系统创建任务
↓
加载测试井数据
↓
执行 W01-W10 Workflow
↓
调用专业 Tool
↓
调用 InterpretationAgent
↓
调用 ValidationAgent
↓
处理异常 / 缺失 / 冲突
↓
形成最终 InterpretationState
↓
生成 JSON 结果
↓
生成 Markdown 报告
↓
Web 页面可查看执行结果
```

## 3. 第一阶段允许使用 Mock 的内容

V0.1 允许：

```text
测试井数据
部分测井专业算法
部分外部业务系统
部分地质 / 录井 / 岩心 / 试油 / 邻井数据
```

## 4. 第一阶段必须真实实现的内容

```text
AgentScope Agent Runtime
内部统一模型调用
MainAgent
InterpretationAgent
ValidationAgent
Workflow 执行
Tool 调用机制
InterpretationState
数据库连接
Redis 连接
状态流转
错误处理
Retry / Rollback 基础机制
Trace / Logging
JSON 输出
Markdown 报告生成
简单 Web 交互
```

## 5. 必须跑通的正常业务链路

至少通过一口测试井完成：

```text
W01 任务初始化与资料加载
↓
W02 数据完整性检查
↓
W03 数据预处理与 QC
↓
W04 岩性识别
↓
W05 储层识别与物性评价
↓
W06 流体识别
↓
W07 油气水层分类
↓
W08 层段划分与有效厚度
↓
W09 多源资料综合验证
↓
W10 最终一致性检查
↓
JSON Result
↓
Markdown Report
```

正常链路必须从开始自动执行到结束，不依赖手工修改中间结果。

## 6. MainAgent 验收标准

MainAgent 至少能够：

1. 接收用户解释请求；
2. 创建或启动解释任务；
3. 触发主 Workflow；
4. 获取 Workflow 当前状态；
5. 在需要时调度 InterpretationAgent；
6. 在需要时调度 ValidationAgent；
7. 识别 Workflow 已完成、失败或需要人工复核；
8. 返回最终任务状态和结果。

MainAgent 不应直接手写孔隙度、Sw 或绕过 Workflow 修改最终解释结论。

## 7. InterpretationAgent 验收标准

至少能够：

1. 读取当前解释上下文；
2. 读取 LithologyResult；
3. 读取 PetrophysicsResult；
4. 根据需要调用允许的专业 Tool；
5. 生成 FluidResult；
6. 生成 LayerClassificationResult；
7. 返回结构化结果；
8. 保留主要 Evidence；
9. 对证据不足输出 Warning / Uncertain；
10. 不编造不存在的数据。

## 8. ValidationAgent 验收标准

至少能够：

1. 读取解释结果；
2. 获取或读取岩心 / 录井 / 试油 / 邻井等验证数据；
3. 判断解释结果与验证数据是否一致；
4. 输出结构化 ValidationResult；
5. 标记 Supporting Evidence；
6. 标记 Conflicting Evidence；
7. 给出 Recommended Action；
8. 必要时提出 Rollback Target；
9. 必要时标记 `REVIEW_REQUIRED`（需要人工复核）。

ValidationAgent 不应直接覆盖解释结果。

## 9. Workflow 验收标准

Workflow 必须具备：

```text
顺序执行
条件判断
状态更新
失败分支
Warning 分支
Blocked 分支
Retry
Rollback
Review
```

至少能够正确处理：

- `SUCCESS`（执行成功）
- `WARNING`（完成但有告警）
- `FAILED`（执行失败）
- `BLOCKED`（被阻断）
- `REVIEW_REQUIRED`（需要人工复核）

## 10. InterpretationState 验收标准

至少保存：

```text
task
well
raw_data
processed_data
qc_result
lithology_result
petrophysics_result
fluid_result
layer_classification
interval_result
validation_result
missing_data
warnings
errors
current_step
completed_steps
review_required
```

必须保证 Workflow 当前步骤与 State 一致、已完成步骤可追踪、Agent 输出写入正确位置、Tool 失败不会产生伪造成功结果、Rollback 后状态可识别重新执行过程。

## 11. Tool 验收标准

至少选择若干代表 Tool，例如：

```text
get_well_data
calculate_porosity
calculate_sw
merge_intervals
```

证明：

```text
Agent / Workflow
↓
Tool Interface
↓
Mock Implementation
```

可以正常运行。

Mock → Real 替换时，上层调用方原则上不需要修改核心逻辑。

## 12. 数据库验收标准

第一阶段数据库真实接入。

至少证明：

1. 数据库连接成功；
2. Task 可以持久化；
3. 关键解释结果可以持久化；
4. Report 可以持久化；
5. 程序重启后能够查询历史 Task 基础信息。

## 13. Redis 验收标准

第一阶段 Redis 真实接入。

至少证明：

1. Redis 连接成功；
2. 可以保存运行时状态或缓存；
3. 运行中的任务能够使用 Redis；
4. Redis 不直接散落在业务代码中；
5. Redis 异常能够被识别和记录。

## 14. 模型访问层验收标准

内部统一模型必须通过统一访问层调用：

```text
Agent
↓
Model Interface
↓
Internal Model Adapter
↓
内部统一模型
```

禁止各 Agent 分别自行实现模型调用逻辑。

## 15. Web 页面验收标准

至少支持：

1. 发起一次解释任务；
2. 选择或输入测试井；
3. 查看任务状态；
4. 查看当前 Workflow 步骤；
5. 查看最终 JSON；
6. 查看 Markdown 报告；
7. 基础错误信息可见。

第一阶段不要求复杂 UI、完整权限、多租户或专业前端工程体系。

## 16. JSON 输出验收标准

最终 JSON 至少包含：

```text
Task 信息
Well 信息
关键阶段解释结果
Layer Classification
Interval Result
Validation Result
Warnings
Errors
Final Status
```

结构必须稳定，能够被程序再次读取。

## 17. Markdown 报告验收标准

至少表达：

```text
井基本信息
数据情况
QC 概况
岩性结果
储层及物性结果
流体识别结果
油气水层分类
层段结果
综合验证情况
Warning / Missing Data
最终解释结论
```

报告内容必须来自已有 InterpretationState，不得凭空补造不存在的数据。

## 18. Trace 验收标准

第一阶段至少追踪：

```text
Task
↓
Workflow Step
↓
Agent Call
↓
Tool Call
↓
Tool Result
↓
Agent Result
↓
State Change
↓
Final Result
```

至少能够回答一次解释任务经过哪些主要步骤，以及某个专业 Tool 是否被成功调用。

## 19. Logging 验收标准

至少记录：

```text
服务启动
任务创建
Workflow 执行
Agent 异常
Tool 异常
数据库异常
Redis 异常
模型调用异常
```

日志不得包含敏感密钥。

## 20. 必测异常场景

### Case 01：正常任务

Required 数据完整。

预期：

```text
W01 → W10 → `SUCCESS`（执行成功）
```

并生成 JSON 和 Markdown。

### Case 02：缺少 Recommended 数据

预期 `WARNING`（完成但有告警），Workflow 继续执行，最终报告中可见缺失信息。

### Case 03：缺少 Required 数据

预期对应步骤 `BLOCKED`（被阻断），不得由 LLM 编造结果继续执行，并记录 missing_data 和 affected_step。

### Case 04：Tool 调用失败

预期：

```text
ToolError
↓
Retry
↓
达到限制
↓
`FAILED`（执行失败）/ `REVIEW_REQUIRED`（需要人工复核）
```

不能静默失败。

### Case 05：模型调用失败

模拟 Timeout / Error，预期有错误记录、按策略 Retry、达到上限后明确失败，并且 Workflow 不产生假的 Agent 结果。

### Case 06：Validation 发现轻微冲突

预期：

```text
PARTIAL_CONFLICT（部分冲突）
↓
WARNING（完成但有告警）
↓
允许进入最终检查
```

### Case 07：Validation 发现严重冲突

当前实现预期：

```text
SERIOUS_CONFLICT（严重冲突）
↓
REVIEW_REQUIRED（需要人工复核）
```

当前版本不会自动回退 W06/W07；自动 Rollback（回退重算）属于后续能力。

### Case 08：达到最大 Rollback 次数

预期停止自动循环，状态 `REVIEW_REQUIRED`（需要人工复核）。

## 21. Retry / Rollback 验收标准

必须证明 Retry 和 Rollback：

- 有次数限制；
- 有原因；
- 有记录；
- 不会无限循环。

至少可以查询 retry_count、rollback_count、rollback_reason。

## 22. 人工复核状态验收

V0.1 至少支持 `REVIEW_REQUIRED`（需要人工复核）逻辑状态。

系统需要能够停止自动执行、保存当前状态并记录为什么需要 Review。

第一阶段不要求建设完整人工审批系统。

## 23. 自动化测试最低要求

至少包括：

```text
Unit Test
Workflow Integration Test
Tool Contract Test
Agent Structured Output Test
```

建议最少覆盖正常路径、缺失数据、Tool Failure、Validation Conflict。

## 24. 代码质量验收

至少满足：

```text
模块职责清晰
核心模块可单独测试
没有明显循环依赖
Mock 与 Real Interface 隔离
配置没有大量散落硬编码
Agent 与业务算法解耦
数据库 / Redis 有 Adapter 边界
```

## 25. 不属于 V0.1 验收范围

第一阶段不作为验收阻塞项：

```text
生产级测井解释准确率
所有专业算法真实实现
完整 RAG
完整长期 Memory
复杂多 Agent 协商
生产级权限系统
高可用架构
Kubernetes
MQ
完整 CI/CD
复杂前端
性能容量优化
正式 Word/PDF 报告
```

## 26. V0.1 最终演示场景

至少准备一口标准测试井，演示：

1. 打开 Web 页面；
2. 选择测试井；
3. 提交“开始解释”；
4. 系统创建 Task；
5. MainAgent 启动 Workflow；
6. 页面可看到 Workflow 执行；
7. 调用 Mock / Real Tool；
8. InterpretationAgent 完成解释；
9. ValidationAgent 完成验证；
10. Final Check；
11. Task `SUCCESS`（执行成功）；
12. 查看 JSON；
13. 查看 Markdown Report；
14. 查看基础 Trace。

## 27. V0.1 最终成功标准

只有同时满足以下条件，才能认为技术骨架跑通：

```text
[ ] 一口标准测试井可以从 W01 自动运行至 W10
[ ] MainAgent 可以正常启动和调度 Workflow
[ ] InterpretationAgent 被真实调用
[ ] ValidationAgent 被真实调用
[ ] 至少若干 Tool 通过标准 Contract 被调用
[ ] InterpretationState 全流程有效
[ ] 数据库真实可用
[ ] Redis 真实可用
[ ] 内部统一模型真实可调用
[ ] 正常路径能够生成 JSON
[ ] 正常路径能够生成 Markdown 报告
[ ] Web 页面能够完成基本交互
[ ] 缺失数据场景可以正确处理
[ ] Tool 失败场景可以正确处理
[ ] Validation Conflict 可以触发受控 Review（人工复核）；未来启用 Rollback 时需满足受控回退规则
[ ] Retry / Rollback 不会无限循环
[ ] Trace 可以看到主要执行链
[ ] 核心测试通过
[ ] 同一测试井可以重复稳定执行
```

## 28. 专业准确率的后续阶段

V0.1 完成之后，再进入：

```text
Mock Tool
↓
真实专业算法

Mock Data
↓
真实脱敏井数据

流程跑通
↓
专业准确率评估
```

之后重点评估：

```text
岩性识别准确率
储层识别准确率
物性参数误差
流体识别准确率
油气水层分类准确率
层段划分准确率
报告完整性
专家一致率
```

因此 V0.1 验收的是技术架构和业务链路，不等于最终专业能力验收。

## 29. 文档定位

本文档是 V0.1 开发和验收的统一标准。

后续每个 Codex Task 都应明确：

> 当前 Task 对 MVP 验收标准中的哪些条目负责。

项目最终进入 V0.1 验收时，应按照本文档逐项检查，不以“代码数量”或“Agent 数量”判断项目是否完成。
