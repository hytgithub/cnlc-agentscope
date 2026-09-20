# 测井解释 Agent V0.1 总体技术架构

## 1. 文档目的

本文档定义测井解释 Agent V0.1 的总体技术架构。

本文档建立在以下文档基础上：

```text
docs/00-project-context.md
docs/01-business-workflow.md
docs/02-agent-tool-boundary.md
docs/mvp-acceptance.md
AGENTS.md
```

目标是将已经确定的：

- 业务 Workflow；
- Agent 职责；
- Tool 边界；
- 状态管理；
- 数据持久化；
- 模型调用；
- Trace；
- Web 交互；

组合成一套可以由 Codex 逐步实施的技术架构。

V0.1 第一目标仍然是：

> 跑通完整测井解释 Agent 技术链路。

## 2. 技术基线

V0.1 采用以下技术基线。

| 类别 | 技术 |
|---|---|
| 开发语言 | Python 3.11 |
| Agent Framework | AgentScope 2.x |
| Agent Service | AgentScope Agent Service |
| Web | AgentScope 配套 Web UI |
| API | AgentScope Agent Service / FastAPI |
| 数据库 | PostgreSQL |
| ORM | SQLAlchemy 2.x |
| 数据库迁移 | Alembic |
| Redis | Redis |
| Redis Client | redis-py asyncio |
| Schema | Pydantic |
| 配置管理 | pydantic-settings |
| 测试 | pytest |
| 代码检查 | ruff |
| 类型检查 | mypy，第一阶段基础启用 |
| Trace | OpenTelemetry 抽象 |
| 日志 | Python logging / structlog 类结构化日志方案 |
| 模型 | 内部统一模型 |
| 依赖管理 | uv + pyproject.toml |

AgentScope 当前要求 Python 3.11 或更高版本，因此 V0.1 统一采用 Python 3.11。

## 3. 总体架构原则

整个系统遵循：

```text
Agent
负责决策

Workflow
负责流程

Tool
负责执行

Domain Algorithm
负责确定性专业计算

InterpretationState
负责业务状态

PostgreSQL
负责长期持久化

Redis
负责运行时状态

Model Gateway
负责统一模型访问

Telemetry
负责可观测性
```

核心原则：

> 不把所有能力都实现成 Agent。

## 4. 总体系统架构

整体架构：

```text
┌──────────────────────────────────────────────┐
│                  Web UI                      │
│          AgentScope Web Interface            │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│              Agent Service / API             │
│                                             │
│       Session / Request / Task API           │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│                  MainAgent                   │
│                                             │
│       Planner + Orchestrator                 │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│          Interpretation Workflow             │
│                                             │
│ W01 → W02 → W03 → W04 → W05                │
│                     ↓                       │
│            InterpretationAgent              │
│               W06 + W07                     │
│                     ↓                       │
│                    W08                      │
│                     ↓                       │
│             ValidationAgent                 │
│                    W09                      │
│                     ↓                       │
│                    W10                      │
└───────────┬───────────────────┬──────────────┘
            │                   │
            ▼                   ▼
      ┌──────────┐        ┌─────────────┐
      │  Tools   │        │ ModelGateway│
      └────┬─────┘        └──────┬──────┘
           │                     │
           ▼                     ▼
   Domain Algorithms       Internal Model
   Data Repository
   External Services
```

底层基础设施：

```text
               Application Layer
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
 PostgreSQL         Redis        OpenTelemetry
        │              │              │
 Persistent       Runtime        Trace / Metrics
   State            State
```

## 5. 分层架构

系统建议划分为：

```text
Interface Layer
Application Layer
Agent Layer
Workflow Layer
Domain Layer
Tool Layer
Infrastructure Layer
```

## 6. Interface Layer

负责系统外部交互。

主要包括：

```text
Web UI
API
CLI / Dev Runner
```

第一阶段重点：

```text
AgentScope Web UI
+
Agent Service
```

用于：

- 发起解释任务；
- 查看 Session；
- 查看 Agent 执行；
- 查看 Tool Call；
- 查看结果；
- 查看 JSON；
- 查看 Markdown Report。

第一阶段不另外建设复杂 Vue / React 前端。

## 7. Application Layer

Application Layer 负责：

```text
Task 生命周期
Use Case 编排
Workflow 启动
事务边界
状态持久化协调
```

候选服务：

```text
InterpretationTaskService
WorkflowService
ReportService
StateService
```

Application Layer 不负责具体测井算法。

## 8. Agent Layer

V0.1 Agent Layer 包含：

```text
MainAgent
InterpretationAgent
ValidationAgent
```

## 9. MainAgent

MainAgent 定位：

```text
Planner
+
Orchestrator
```

职责：

```text
用户请求理解
↓
确认任务类型
↓
创建解释 Task
↓
启动 InterpretationWorkflow
↓
观察 Workflow 状态
↓
协调异常 / Review
↓
返回最终结果
```

MainAgent 不逐步手写执行 W01-W10，而是启动 Workflow。

## 10. MainAgent 与 Workflow 控制权

采用：

```text
MainAgent
控制“任务级别”

Workflow
控制“业务步骤级别”
```

例如：

MainAgent 决定：

```text
启动单井解释任务
```

Workflow 决定：

```text
W01
→
W02
→
W03
```

W09 出现冲突时，由 Workflow 负责：

```text
W09 → W06
```

而不是让 MainAgent 自由生成流程。

## 11. InterpretationAgent

InterpretationAgent 负责：

```text
W06 流体识别
+
W07 油气水层分类
```

核心执行逻辑：

```text
读取 Context
↓
分析已有证据
↓
判断是否需要 Tool
↓
调用 Tool
↓
获得结构化结果
↓
继续综合推理
↓
生成结构化解释结果
```

## 12. InterpretationAgent Context

原则上不直接把完整 InterpretationState 全部塞给模型。

采用：

```text
InterpretationState
↓
ContextBuilder
↓
InterpretationContext
↓
InterpretationAgent
```

ContextBuilder 只选择当前任务需要的数据，例如：

```text
LithologyResult
PetrophysicsResult
RT / RXO
Sw
QC Warning
Mud Logging
Interval Info
```

这样降低 Context 长度、无关信息、Token 消耗和 Prompt 干扰。

## 13. ValidationAgent

ValidationAgent 独立执行：

```text
测井解释结果
+
岩心
+
录井
+
试油
+
邻井
+
地质资料
↓
一致性验证
```

输出：

```text
ValidationResult
```

ValidationAgent 不直接修改 FluidResult 和 LayerClassificationResult，而是给 Workflow 返回：

```text
validation_status
conflicting_evidence
rollback_target
recommended_action
```

## 14. Workflow Layer

Workflow 是整个业务系统的骨架。

V0.1 核心 Workflow：

```text
InterpretationWorkflow
```

内部：

```text
W01
│
▼
W02
│
▼
W03
│
▼
W04
│
▼
W05
│
▼
W06
│
▼
W07
│
▼
W08
│
▼
W09
│
▼
W10
```

## 15. AgentScope Pipeline 的使用原则

V0.1 可以优先评估 AgentScope Pipeline 承载确定性流程。

但需要保持一个原则：

```text
Business Workflow Definition
```

不能完全绑定：

```text
AgentScope Pipeline API
```

推荐增加 Workflow Interface，例如：

```text
InterpretationWorkflow
```

内部再使用 AgentScope Pipeline 实现。

## 16. Workflow Node

每个节点统一实现概念：

```text
WorkflowNode
```

至少包含：

```text
step_id
execute()
precondition()
result
status
```

逻辑：

```text
读取 State
↓
Precondition
↓
Execute
↓
Result
↓
Update State
↓
Select Next
```

## 17. Workflow State Machine

节点状态：

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

统一定义：

```text
StepStatus
```

## 18. Retry

Retry 面向：

```text
Tool Failure
Model Timeout
Temporary Infrastructure Failure
```

Retry 必须有限次数、可配置、记录原因和记录次数。

## 19. Rollback

Rollback 面向：

```text
业务解释结果需要重新执行
```

例如：

```text
W09
↓
发现试油与流体解释严重冲突
↓
rollback_target = W06
↓
W06
↓
W07
↓
W08
↓
W09
```

Retry 和 Rollback 必须严格区分。

## 20. Retry 与 Rollback 区别

Retry：

```text
同一步重新执行
```

Rollback：

```text
返回之前的业务节点重新解释
```

## 21. 防止无限循环

统一配置：

```text
MAX_TOOL_RETRY
MAX_MODEL_RETRY
MAX_WORKFLOW_ROLLBACK
```

超过上限：

```text
REVIEW_REQUIRED
```

## 22. Tool Layer

Tool Layer 是 Agent / Workflow 与具体执行能力之间的边界。

推荐：

```text
Agent
↓
Tool Contract
↓
Tool Implementation
↓
Domain Algorithm / Infrastructure
```

## 23. Tool 分类

V0.1 Tool 分为：

```text
Data Tools
QC Tools
Petrophysics Tools
Interpretation Tools
Interval Tools
Knowledge Tools
```

## 24. Data Tools

例如：

```text
GetWellInfoTool
GetLogDataTool
GetCoreDataTool
GetMudLoggingDataTool
GetWellTestDataTool
GetOffsetWellDataTool
```

内部访问 Repository，而不是直接 SQL。

## 25. Petrophysics Tools

例如：

```text
CalculateVshTool
CalculatePorosityTool
CalculatePermeabilityTool
CalculateSwTool
```

内部：

```text
Tool
↓
Domain Algorithm
```

V0.1 可以使用 Mock Algorithm，未来替换为 Real Algorithm。

## 26. Tool Contract

所有 Tool 使用统一设计：

```text
Input
Output
Status
Warning
Error
Metadata
```

概念结构：

```json
{
  "status": "SUCCESS",
  "data": {},
  "warnings": [],
  "errors": [],
  "metadata": {}
}
```

具体 Schema 后续 Tool Design 阶段确定。

## 27. Domain Layer

Domain Layer 是整个系统最需要与 AgentScope 解耦的部分。

包含：

```text
InterpretationState
Well
LogCurve
Interval
LithologyResult
PetrophysicsResult
FluidResult
LayerClassificationResult
ValidationResult
WorkflowExecution
```

这些对象原则上不依赖 AgentScope。

## 28. Domain Algorithm

专业算法放：

```text
domain/algorithms/
```

例如：

```text
vsh.py
porosity.py
permeability.py
water_saturation.py
interval.py
```

Domain Algorithm 不直接依赖 AgentScope、LLM、Redis、PostgreSQL 或 FastAPI。

## 29. InterpretationState

InterpretationState 是单次测井解释任务的统一业务状态。

建议逻辑结构：

```text
InterpretationState
├── TaskContext
├── WellContext
├── RawData
├── ProcessedData
├── QCResult
├── LithologyResult
├── PetrophysicsResult
├── FluidResult
├── LayerClassificationResult
├── IntervalResult
├── ValidationResult
├── WorkflowState
├── Warnings
├── Errors
└── ReviewState
```

正式 Schema 后续单独设计。

## 30. InterpretationState 生命周期

运行期间：

```text
Redis
```

保存 Active InterpretationState。

长期结果：

```text
PostgreSQL
```

保存：

```text
Task
Final Result
Workflow Execution
Report
```

## 31. PostgreSQL 职责

PostgreSQL 用于长期、可靠、可查询的数据。

V0.1 候选表：

```text
interpretation_task
well
interpretation_result
workflow_execution
workflow_step_execution
report
agent_execution
tool_execution
```

不要求第一阶段建立非常复杂的数据模型。

## 32. PostgreSQL Repository

业务代码不直接使用 ORM Session，而采用：

```text
Repository Interface
↓
PostgreSQL Repository
```

例如：

```text
TaskRepository
WellRepository
ResultRepository
ReportRepository
```

## 33. SQLAlchemy

ORM：

```text
SQLAlchemy 2.x
```

采用 Async SQLAlchemy，以便与 Agent / API 异步执行模式配合。

数据库迁移：

```text
Alembic
```

## 34. Redis 职责

Redis 主要承担：

```text
Active Task State
Workflow Runtime State
Short-lived Context
Session Cache
Distributed Lock
Temporary Result Cache
```

Redis 不作为最终业务事实的唯一存储。

## 35. Redis StateStore

业务层通过：

```text
StateStore Interface
```

访问 Redis。

例如：

```text
InterpretationStateStore
↓
RedisInterpretationStateStore
```

禁止 Workflow 到处直接调用 redis SDK。

## 36. PostgreSQL 与 Redis 的边界

简单原则：

```text
以后还需要查
↓
PostgreSQL
```

```text
当前正在运行
↓
Redis
```

## 37. Model Layer

所有 Agent 和 LLM 功能统一通过：

```text
ModelGateway
```

调用模型。

架构：

```text
MainAgent
InterpretationAgent
ValidationAgent
ReportGenerator
        │
        ▼
   ModelGateway
        │
        ▼
InternalModelAdapter
        │
        ▼
  内部统一模型
```

## 38. ModelGateway 职责

负责：

```text
模型调用
Timeout
Retry
Error Mapping
Request Metadata
Trace
模型配置
```

Agent 不直接知道内部模型 URL、Token、底层协议或具体 SDK。

## 39. 模型未来扩展

未来可以实现：

```text
ModelGateway
├── MainModelAdapter
├── LightweightModelAdapter
└── OtherModelAdapter
```

但 V0.1 只接：

```text
InternalModelAdapter
```

## 40. Report Layer

V0.1 不建立 ReportAgent。

采用：

```text
InterpretationState
↓
ReportAssembler
↓
ReportContext
↓
LLM
↓
Markdown
```

同时：

```text
InterpretationState
↓
ResultSerializer
↓
JSON
```

## 41. ReportAssembler

ReportAssembler 负责结构化数据整理，而 LLM 只负责自然语言组织，避免 LLM 自己重新解释整口井。

## 42. Web 架构

V0.1 优先复用：

```text
AgentScope Agent Service
+
AgentScope Web UI
```

V0.1 不另外开发完整前端系统。

## 43. Web V0.1 功能

至少：

```text
选择 Mock Well
输入解释任务
启动任务
查看 Agent 执行
查看 Tool Call
查看状态
查看 JSON
查看 Markdown
```

## 44. Trace 架构

V0.1 采用：

```text
Telemetry Interface
↓
OpenTelemetry
```

AgentScope 相关运行事件和项目自身 Workflow / Tool / State 事件统一纳入 Trace。

## 45. Trace Span 建议

Trace 层级：

```text
InterpretationTask
├── Workflow
├── W01
├── W02
├── W03
├── ...
├── InterpretationAgent
│   ├── Model Call
│   └── Tool Call
├── ValidationAgent
│   ├── Model Call
│   └── Tool Call
└── Report
```

## 46. Trace Backend

V0.1 不将业务代码绑定某个具体 Trace 产品。

可以接：

```text
Jaeger
Phoenix
其他 OpenTelemetry Backend
```

通过配置切换。

当前核心要求是：

> Trace 数据可以被正常产生。

## 47. Logging

日志与 Trace 分离。

日志用于：

```text
系统启动
异常
数据库
Redis
API
Debug
```

Trace 用于：

```text
Agent 执行链
Workflow 执行链
Tool 调用
Model 调用
State Change
```

## 48. 异常体系

统一异常基础类：

```text
ApplicationError
```

候选子类：

```text
DataError
ToolError
ModelError
WorkflowError
InfrastructureError
ValidationError
```

## 49. 错误传播

例如：

```text
Domain Algorithm
↓
ToolError
↓
Workflow
↓
Retry
↓
FAILED / REVIEW
```

Agent 不应该通过猜测来处理异常。

## 50. 配置架构

采用：

```text
pydantic-settings
```

配置：

```text
AppSettings
DatabaseSettings
RedisSettings
ModelSettings
WorkflowSettings
TelemetrySettings
```

## 51. 环境变量

例如：

```text
DATABASE_URL
REDIS_URL
MODEL_BASE_URL
MODEL_API_KEY
MODEL_NAME
OTEL_EXPORTER_OTLP_ENDPOINT
```

`.env` 仅开发环境使用。

敏感数据不提交 Git。

## 52. 推荐项目目录

V0.1 推荐：

```text
cnlc-agentscope/
│
├── AGENTS.md
├── README.md
├── pyproject.toml
├── .env.example
│
├── docs/
│   ├── 00-project-context.md
│   ├── 01-business-workflow.md
│   ├── 02-agent-tool-boundary.md
│   ├── 03-system-architecture.md
│   └── mvp-acceptance.md
│
├── src/
│   └── cnlc_agent/
│       ├── agents/
│       │   ├── main_agent.py
│       │   ├── interpretation_agent.py
│       │   └── validation_agent.py
│       ├── workflows/
│       │   ├── interpretation_workflow.py
│       │   ├── steps/
│       │   └── policies/
│       ├── domain/
│       │   ├── models/
│       │   ├── states/
│       │   ├── results/
│       │   ├── enums/
│       │   └── algorithms/
│       ├── tools/
│       │   ├── data/
│       │   ├── qc/
│       │   ├── petrophysics/
│       │   ├── interpretation/
│       │   └── interval/
│       ├── application/
│       │   ├── services/
│       │   └── context/
│       ├── infrastructure/
│       │   ├── database/
│       │   ├── redis/
│       │   ├── model/
│       │   ├── telemetry/
│       │   └── repositories/
│       ├── reports/
│       ├── api/
│       ├── config/
│       └── main.py
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── workflow/
│   └── fixtures/
│
└── mock_data/
```

## 53. 目录设计原则

采用：

> 轻量 DDD + Agent 架构。

重点隔离：

```text
Domain
Application
Infrastructure
```

同时把 Agent、Workflow、Tool 作为 Agent 系统一级概念。

## 54. 为什么不采用传统简单 Python 目录

如果全部写成：

```text
utils.py
service.py
agent.py
tool.py
```

随着 Tool 和 Agent 增加，会快速出现职责混乱、循环依赖、业务代码与 AgentScope 耦合和测试困难。

因此 V0.1 就应该建立明确模块边界。

## 55. 为什么不采用完整重型 DDD

V0.1 第一目标是快速跑通，不需要大量 Aggregate、Domain Event、复杂 Repository Factory 或复杂 CQRS。

只保留真正有价值的：

```text
Domain Model
Repository Interface
Adapter
Application Service
Infrastructure Isolation
```

## 56. Mock 架构

Mock 能力必须正式化。

例如：

```text
PorosityCalculator
├── MockPorosityCalculator
└── RealPorosityCalculator
```

或者：

```text
PorosityTool
├── MockPorosityTool
└── RealPorosityTool
```

由配置选择实现。

## 57. Mock Well Data

统一放：

```text
mock_data/
```

例如：

```text
mock_data/
└── well_001/
    ├── well.json
    ├── logs.json
    ├── geology.json
    ├── mud_logging.json
    ├── core.json
    └── well_test.json
```

具体数据格式后续 Schema 阶段确定。

## 58. 横向扩展

系统需要预留横向扩展能力。

核心要求：

```text
Agent 实例不依赖本地内存保存唯一状态
```

运行状态进入 Redis，长期状态进入 PostgreSQL。

## 59. V0.1 暂不实现复杂分布式系统

虽然支持未来横向扩展，但 V0.1 不建设：

```text
Kubernetes
复杂 MQ
复杂分布式调度
自动扩缩容
```

只要求架构不阻止未来扩展。

## 60. RAG

RAG 未来用于解释规范、专业知识、区块经验和历史报告。

V0.1 不作为主链路必选能力。

第一阶段预留：

```text
KnowledgeTool Interface
```

即可。

## 61. Memory

V0.1 不优先实现复杂长期 Agent Memory。

当前核心：

```text
InterpretationState
+
Session Context
```

未来再增加 Domain Knowledge Memory、Historical Well Memory、User Preference Memory。

## 62. 安全边界

V0.1 至少保证：

```text
API Key 不进仓库
数据库密码不进仓库
日志不打印 Token
Agent 不直接执行任意 SQL
Agent Tool 权限明确
Tool 白名单
```

## 63. Tool 权限

不同 Agent 使用不同 Tool 集合。

例如：

MainAgent：

```text
Task Tools
Workflow Tools
```

InterpretationAgent：

```text
Petrophysics Tools
Interpretation Tools
部分 Data Tools
```

ValidationAgent：

```text
Core Data Tool
Mud Logging Tool
Well Test Tool
Offset Well Tool
```

具体 Tool Permission 后续设计。

## 64. 测试架构

测试分层：

```text
Unit
↓
Tool Contract
↓
Agent
↓
Workflow Integration
↓
End-to-End
```

## 65. Unit Test

重点测试：

```text
Domain Model
Algorithm
State
Repository
Tool
```

不需要模型即可完成。

## 66. Agent Test

使用 Mock Model 和 Mock Tool，测试结构化输入、结构化输出、Tool Selection 和 Error Handling。

## 67. Workflow Test

至少包含：

```text
Normal
Missing Data
Tool Failure
Model Failure
Validation Conflict
Rollback Limit
```

## 68. End-to-End Test

最终：

```text
Mock Well
↓
Agent Service
↓
MainAgent
↓
Workflow
↓
Tool
↓
InterpretationAgent
↓
ValidationAgent
↓
Report
```

跑通。

## 69. V0.1 部署方式

第一阶段推荐：

```text
Docker Compose
```

组件：

```text
Agent Application
PostgreSQL
Redis
Trace Backend
```

Web UI 使用 AgentScope 配套 Web UI。

## 70. 为什么采用 Docker Compose

因为第一阶段需要真实 PostgreSQL、Redis、Trace。

Docker Compose 可以快速建立统一开发环境，但不引入 Kubernetes。

## 71. 部署拓扑

V0.1：

```text
Developer Browser
      │
      ▼
AgentScope Web UI
      │
      ▼
Agent Service
      │
 ┌────┼───────────────┐
 ▼    ▼               ▼
DB   Redis       Internal Model
 │
 ▼
PostgreSQL
```

同时：

```text
Agent Service
↓
OpenTelemetry
↓
Trace Backend
```

## 72. 核心调用链

一次解释任务：

```text
Web
↓
Agent Service
↓
MainAgent
↓
TaskService
↓
InterpretationWorkflow
↓
W01 DataTool
↓
W02 Completeness
↓
W03 QCTool
↓
W04 Lithology
↓
W05 Petrophysics Tools
↓
InterpretationAgent
↓
W06 Fluid
↓
W07 Layer Classification
↓
W08 Interval Tool
↓
ValidationAgent
↓
W09 Validation
↓
W10 Final Check
↓
ReportAssembler
↓
ModelGateway
↓
Markdown Report
```

## 73. 状态调用链

```text
Workflow
↓
InterpretationState
↓
Redis StateStore
```

阶段完成：

```text
InterpretationState
↓
Persistence Service
↓
PostgreSQL
```

## 74. Trace 调用链

```text
Request
↓
Task Trace
↓
Workflow Trace
↓
Agent Trace
↓
Tool Trace
↓
Model Trace
↓
State Change
```

必须能够通过 task_id 关联。

## 75. 核心 ID

建议统一：

```text
task_id
well_id
session_id
workflow_execution_id
step_execution_id
agent_execution_id
tool_execution_id
trace_id
```

方便 DB、Redis、Log 和 Trace 关联。

## 76. Codex 第一阶段实现边界

总体架构完成后，Codex 第一轮只建立：

```text
项目结构
依赖配置
基础 Domain Model
InterpretationState Skeleton
Agent Skeleton
Workflow Skeleton
Tool Contract Skeleton
Model Gateway Interface
Repository Interface
Redis StateStore Interface
Telemetry Interface
Mock Data Loader
最小运行入口
```

不直接实现全部测井业务。

## 77. 第一次 End-to-End Skeleton

第一轮骨架至少能模拟：

```text
Mock Well
↓
MainAgent
↓
Workflow
↓
Mock Tool
↓
Mock Interpretation Result
↓
Mock Validation
↓
JSON
↓
Markdown
```

后续 Task 再逐个替换。

## 78. 架构冻结规则

本文档完成后：

```text
V0.1 Architecture Baseline
```

视为冻结。

Codex 不得自行：

```text
增加 Agent
更换数据库
删除 Redis
改变 Workflow
修改核心模块边界
```

如果确实需要修改：

```text
Architecture Issue
```

先提出问题。

## 79. 当前仍未冻结的设计

以下后续阶段继续设计：

```text
InterpretationState 完整 Schema
Well JSON Schema
Tool Contract 详细 Schema
Agent Prompt
Agent ContextBuilder
Tool Permission Matrix
专业算法
业务判断规则
RAG
Evaluation
```

这些不阻塞 Codex 建立第一版工程骨架。

## 80. V0.1 最终架构总结

系统总体采用：

```text
AgentScope
+
Workflow Driven
+
3 Core Agents
+
Tool First
+
Domain Algorithm
+
InterpretationState
+
PostgreSQL
+
Redis
+
Internal Model Gateway
+
OpenTelemetry
+
AgentScope Web UI
```

核心架构思想：

```text
用户
↓
MainAgent
↓
Workflow
↓
Tool / Professional Agent
↓
InterpretationState
↓
Validation
↓
Report
```

系统优先保证：

```text
业务流程稳定
Agent 职责清晰
专业算法可替换
运行状态可追踪
Mock 可替换 Real
支持未来横向扩展
代码能够由 Codex 分阶段实施
```

V0.1 不追求一次性构建完整生产系统。

第一阶段唯一核心目标：

> **把正确的架构真正跑起来。**
