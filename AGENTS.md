# AGENTS.md

## 1. 文档目的

本文档定义 Codex 在本项目中的开发行为规范。

Codex 的主要职责是：

> 按照已经确认的项目上下文、业务 Workflow 和架构设计完成代码实现、测试和验证。

Codex 不负责在开发过程中自行重新设计整个系统。

如果实现过程中发现现有设计存在问题，应优先记录问题并反馈，而不是未经确认直接修改核心架构。

## 2. 必须优先阅读的项目文档

开始任何开发 Task 之前，必须先阅读与当前任务相关的项目文档。

核心文档包括：

```text
docs/00-project-context.md
docs/01-business-workflow.md
docs/02-agent-tool-boundary.md
docs/03-system-architecture.md
docs/11-status-enum-glossary.md
docs/mvp-acceptance.md
AGENTS.md
```

项目文档的优先级高于 Codex 自行推断的设计。

## 3. 当前系统核心原则

```text
Agent 负责决策
Workflow 负责流程
Tool 负责执行
Algorithm 负责确定性计算
LLM 负责理解、综合和文本生成
```

禁止为了增加 Agent 数量而人为拆分业务。

## 4. 架构修改规则

未经当前 Task 明确要求，Codex 不得擅自：

- 新增或删除核心 Agent；
- 修改业务主 Workflow；
- 改变 W01-W10 的核心业务顺序；
- 改变 MainAgent、InterpretationAgent、ValidationAgent 核心职责；
- 将确定性专业算法替换为 LLM 推理；
- 改变 InterpretationState 的整体定位；
- 直接替换数据库 / Redis / Trace 等核心基础设施方案；
- 进行大规模跨模块重构。

如果 Codex 认为当前设计无法实现、存在明显冲突、严重耦合或明显不符合 AgentScope 实现方式，应记录 Architecture Issue，包括问题描述、涉及模块、当前设计、问题原因、影响范围、建议方案和是否阻塞当前 Task。

## 5. Task 边界原则

Codex 每次只完成当前 Task 明确要求的工作。

除非完成当前 Task 所必需，否则不要：

- 添加大型新依赖；
- 新增新的基础设施；
- 修改无关模块；
- 重构整个项目；
- 提前实现未来 Task。

每个 Task 应可以独立审查、测试和回滚。

## 6. Agent 开发规则

当前 V0.1 核心 Agent：

```text
MainAgent
InterpretationAgent
ValidationAgent
```

未经架构文档调整，不新增核心业务 Agent。

### 6.1 MainAgent

定位：

```text
Planner + Orchestrator
```

主要负责用户任务理解、任务规划、Workflow 启动、状态检查、专业 Agent 调度、异常协调和最终结果汇总。

### 6.2 InterpretationAgent

V0.1 主要承担：

```text
W06 流体识别
W07 油气水层分类
```

不得把孔隙度、渗透率、Sw 等确定性计算直接放入 LLM Prompt 中进行估算。

### 6.3 ValidationAgent

主要负责岩心、录井、试油、邻井、多证据一致性、冲突识别、回退建议和人工复核建议。

ValidationAgent 原则上不直接覆盖 InterpretationAgent 的结构化解释结果，应输出 ValidationResult，由 Workflow 决定后续动作。

## 7. Agent 输出规则

关键业务输出必须优先采用结构化对象。

应包含类似：

```text
result
evidence
conflicts
missing_evidence
warnings
recommended_action
```

具体 Schema 以项目 Schema 文档为准。

## 8. Agent 通信规则

V0.1 不推荐多个 Agent 之间进行无约束自然语言聊天。

核心业务数据传递优先通过：

```text
InterpretationState
Structured Result
Workflow
```

完成。

## 9. Tool 开发规则

每一个 Tool 必须具有明确 Contract，至少定义：

```text
Tool Name
Responsibility
Input Schema
Output Schema
Error
Timeout
Status
Mock Implementation
Test
```

Tool 职责必须单一。

## 10. Tool 与算法关系

推荐：

```text
Agent / Workflow
↓
Tool
↓
Domain Algorithm
```

算法层原则上应尽量保持纯计算，不直接依赖 AgentScope、Redis、Database、Web 或 LLM。

## 11. Mock Tool 规则

V0.1 允许使用 Mock Tool，但 Mock 必须实现正式 Tool Contract、返回正式 Output Schema、支持测试和未来替换。

推荐：

```text
PorosityTool interface
├── MockPorosityTool
└── RealPorosityTool
```

不要在业务代码中到处写临时 mock 判断。

## 12. 禁止 LLM 替代专业计算

如果已经存在或者计划存在确定性专业算法，则禁止要求 LLM 直接估算。

正确流程：

```text
Agent
↓
calculate_porosity Tool
↓
PorosityResult
↓
Agent 使用结果进行综合判断
```

## 13. Workflow 开发规则

Workflow 负责：

```text
步骤编排
条件判断
状态转换
Retry
Rollback
Review
Error Handling
```

Workflow 不承担复杂专业推理。

当前业务 Workflow W01-W10 不得随意修改，如需调整必须先形成 Architecture Issue。

## 14. Workflow 状态规则

候选统一状态：

- `PENDING`（等待执行）
- `RUNNING`（正在执行）
- `SUCCESS`（执行成功）
- `WARNING`（完成但有告警）
- `FAILED`（执行失败）
- `BLOCKED`（被阻断）
- `REVIEW_REQUIRED`（需要人工复核）
- `SKIPPED`（已跳过）

完整定义见 `docs/11-status-enum-glossary.md`。禁止不同模块自行创建含义重复但命名不同的状态。

### 14.1 文档中的状态/枚举中文标注

设计文档、Task 说明和验收记录中，状态、枚举、动作码、规划原因等稳定代码值首次出现时，必须写成 `CODE（中文名称或中文含义）`，或在紧邻位置提供中文说明表。不得只列英文枚举让读者自行猜测。新增枚举时必须同步更新 `docs/11-status-enum-glossary.md`。

## 15. Retry / Rollback 规则

Retry 和 Rollback 必须有明确原因、次数限制、Trace 和状态记录。超过限制后，应进入 REVIEW_REQUIRED 或明确失败状态，禁止无限执行。

## 16. InterpretationState 开发规则

InterpretationState 是单井解释任务的核心状态对象。

禁止依赖大量全局变量、Agent 私有状态保存最终业务结果、不可追踪的临时缓存或 Prompt 中隐藏的大量关键业务数据。

重要状态变化应能够回答：谁修改、什么时候修改、为什么修改、修改前后是什么。

## 17. 数据库规则

数据库真实接入。

推荐：

```text
Business / Workflow
↓
Repository Interface
↓
Repository Implementation
↓
Database
```

Agent 原则上不直接持有数据库连接。

## 18. Redis 规则

Redis 真实接入。

推荐：

```text
Business
↓
Cache / State Store Interface
↓
Redis Adapter
```

业务代码不得大量直接调用 Redis SDK。

## 19. Model Access 规则

内部统一模型必须通过统一模型访问层调用。

推荐：

```text
Agent / LLM Service
↓
Model Gateway / Model Client Interface
↓
Internal Model Adapter
```

禁止每个 Agent 分别实现自己的模型调用逻辑。

## 20. AgentScope 依赖规则

业务核心对象如 Domain Result、InterpretationState、Tool Contract 应尽可能保持业务独立性，避免和 AgentScope 内部类型产生不必要的深度耦合。

## 21. 配置规则

禁止将模型名称、数据库连接、Redis 地址、Timeout、Retry 次数、Rollback 次数、Log Level 等配置散落写死。

密码、Token、API Key 不得提交到代码仓库。

## 22. 日志和 Trace

普通日志处理系统运行、异常、Debug 和基础设施问题。

Agent Trace 主要处理：

```text
Task
Workflow Step
Agent Call
Tool Call
Tool Result
State Change
Final Result
```

## 23. Error Handling 规则

不得大量使用：

```python
except Exception:
    pass
```

所有异常至少需要分类、日志、Trace，并明确是否可重试以及是否影响 Workflow。

候选错误类型：

```text
DataError
ToolError
ModelError
WorkflowError
InfrastructureError
ValidationError
```

## 24. 测试规则

每一个 Task 完成后必须测试。

至少包括：

```text
Unit Test
必要的 Integration Test
```

关键 Workflow 必须覆盖正常路径、缺失数据路径、Tool 失败路径和 Validation Conflict 路径。

涉及 Web 可见行为的修改，在自动测试通过后必须进行真实页面操作验收。
聊天过程展示至少检查折叠/展开、计时位置、报告可见性、参数修改后的回复和刷新恢复。
不能只检查 DOM 中存在文本或 aria-expanded；必须确认实际操作后正文隐藏/恢复，报告仍显示。

## 25. 不以“代码生成完成”作为验收标准

至少需要确认：

```text
代码能够 import
基础测试通过
主要接口能够调用
没有明显语法错误
当前 Task 的验收条件满足
```

## 26. 代码质量原则

优先：

```text
Readable
Testable
Low Coupling
Clear Responsibility
```

第一阶段避免复杂抽象层、大量无业务价值基类、不必要设计模式和过早优化。

### 26.1 中文注释与文档字符串规范

项目自有代码必须为以下内容补充简洁、准确的中文注释或中文文档字符串：

- 模块、公共类、公共函数及关键数据结构的职责；
- 非直观的业务规则、状态流转、异常分支和安全边界；
- Agent、Workflow、Tool、持久化及模型调用之间的职责边界；
- 为兼容第三方框架、规避副作用或保护敏感数据而采用的特殊实现；
- 暂时使用 Mock、占位实现或待业务确认规则的原因。

注释应解释“为什么这样实现”和“该逻辑的边界”，不要逐行复述显而易见的代码。
后续新增或修改项目自有代码时，应同步维护相关中文注释；第三方上游快照、生成文件、
Schema 导出文件和测试夹具不要求为了注释而改写。

## 27. 依赖管理规则

新增第三方依赖前，应判断是否真的需要、标准库是否可以解决、现有依赖是否已有类似能力。

## 28. 当前阶段禁止提前实现的内容

除非 Task 明确要求，V0.1 阶段不要提前建设：

```text
Kubernetes
高可用集群
完整权限系统
复杂 MQ
完整 CI/CD 平台
完整企业级安全体系
复杂性能容量系统
生产级前端
完整向量数据库平台
```

当前优先目标：

> 跑通测井解释 Agent 主链路。

## 29. 每个 Codex Task 的标准执行流程

1. 阅读当前 Task 指定的项目设计文档；
2. 检查已有代码，不重复建设；
3. 确定影响范围；
4. 按架构文档实现；
5. 运行必要测试；
6. 检查架构偏差；
7. 报告结果。

## 30. 每次 Task 完成后必须报告

必须报告：

1. 本次实现；
2. 新增文件；
3. 修改文件；
4. 核心设计；
5. 测试；
6. 测试结果；
7. 未完成内容；
8. Architecture Issue；
9. 下一阶段依赖。

如没有 Architecture Issue，应明确说明：

```text
No Architecture Issue found.
```

## 31. Codex 不允许自行补造专业业务规则

如果项目文档没有明确油层判断阈值、Sw 阈值、孔隙度标准、有效厚度标准或专业计算公式，Codex 不得自行猜测并写入生产逻辑。

应该保留 Interface、使用 Mock、增加 TODO 并记录待确认业务规则。

## 32. 数据格式不确定时

当前测试井正式数据格式尚未最终确定。

如果开发 Task 发生在 Schema 最终确定之前，应采用：

```text
明确 Interface
+
最小可用 Schema
+
可扩展字段
```

并标记：

```text
Pending final well-data schema.
```

## 33. 项目第一优先级

当前项目第一阶段最重要的目标为：

> 跑通。

在不破坏核心架构原则的前提下，优先保证一个简单、清晰、可测试的 V0.1 实现真正跑通。

## 34. 最终原则

Codex 应始终遵循：

```text
先理解设计
再进行实现
不擅自扩大范围
不擅自修改架构
不使用 LLM 替代确定性算法
每次 Task 独立完成
实现必须测试
问题必须暴露
不要隐藏失败
```
