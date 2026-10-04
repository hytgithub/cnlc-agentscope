# AGENTS.md

## 1. 文档目的

本文档定义 Codex 在本项目中的开发行为规范和文档治理规则。

Codex 的主要职责是：

> 按照当前项目主设计基线、已验证证据和当前 Task 完成代码实现、实验、测试和验证。

Codex 不负责在开发过程中自行重新设计整个系统，也不得因为现有代码与目标设计不同就擅自删除现有安全能力。

如果实现过程中发现现有实现与主设计基线存在冲突，应先区分：
- 目标设计；
- 当前实现；
- 已验证证据；
- 待验证假设。

涉及核心架构变化时，必须形成 Architecture Issue 或明确迁移 Task，并提供测试／实验依据后再实施。

## 2. 必须优先阅读的项目文档

开始任何开发 Task 之前，必须先阅读与当前任务相关的项目文档。

文档治理优先级如下：

1. `AGENTS.md`：开发行为、证据门槛和文档治理；
2. `docs/design/01～05`：项目主设计基线，按“需求 → 目标 → 效果 → 方案 → 测试”组织；
3. `docs/evidence/`：现状事实、运行证据、AgentScope 原生能力实验与架构决策依据；
4. 原有 `docs/*.md` Current Implementation 文档：说明当前代码和历史详细设计；
5. `docs/tasks/`：一次具体实施或实验任务。

建议先阅读：

```text
AGENTS.md
docs/README.md
docs/design/README.md
docs/design/01-测井解释智能体需求规格说明.md
docs/design/02-测井解释智能体建设目标.md
docs/design/03-测井解释智能体预期效果.md
docs/design/04-测井解释智能体技术方案.md
docs/design/05-测井解释智能体测试与验收方案.md
docs/evidence/README.md
docs/11-status-enum-glossary.md
```

按当前 Task 再读取 Current Implementation 和对应 Task 文档。

如果 `docs/design/` 与旧实现文档描述不同：
- `docs/design/` 回答“目标是什么、当前认可的方向是什么”；
- Current Implementation 文档和代码回答“现在实际上怎么实现”；
- 不得静默把旧实现当成永久目标，也不得无迁移证据直接按新目标破坏当前实现。

## 3. 当前系统核心原则

目标架构采用以下职责原则，并通过 AgentScope 原生能力实验逐步验证：

```text
Prompt 管原则
Skill 管方法（仅在实验验证有价值时采用）
Agent 管决策
Tool 管动作
Memory / Context 管对话理解
State 管运行态
Domain 管业务事实
Plan / Task 管当前准备怎么做
Trace 管为什么这么干
```

同时保留不可让模型替代的确定性边界：

```text
权限 / 授权
Task / Execution / InputVersion 归属
版本与并发一致性
真实专业计算与正式业务依赖
参数 Schema 与允许修改范围
Artifact / 文件来源
外部 API 状态、幂等、超时 unknown
正式结果生效与历史保护
```

当前已有 Workflow、Operation、Stage、Task/Execution 等实现属于可复用资产。在没有 A/B 实验、回归测试和迁移计划前不得擅自删除。

禁止为了增加 Agent 数量而人为拆分业务；当前优先使用一个主 Agent 验证自然语言理解、Tool 选择和交互能力。

## 4. 架构修改规则

未经当前 Task 明确要求且缺少证据时，Codex 不得擅自：

- 新增或删除核心 Agent；
- 删除或替换现有 Workflow、Operation、Stage 等核心执行能力；
- 改变 W01-W10 当前完整解释内部业务顺序；
- 删除 MainAgent、InterpretationAgent、ValidationAgent 等当前实现资产；
- 将确定性专业算法、权限、版本或一致性保护替换为 LLM 推理；
- 改变 Task / Execution / InputVersion / Artifact 等权威业务事实模型；
- 直接替换数据库 / Redis / Trace 等核心基础设施方案；
- 进行大规模跨模块重构。

上述内容是**变更门槛**，不是永久冻结旧架构。

当 `docs/design/04` 的目标方案与当前实现不同，允许通过独立实验验证 AgentScope 原生能力。只有满足以下条件后，才可以正式迁移生产代码：

1. 当前 Task 明确要求迁移；
2. `docs/evidence/` 或 Task 记录中存在可重复证据；
3. 明确 KEEP / REPLACE / WRAP / ADD 决策；
4. 不降低权限、版本、专业结果和数据一致性安全；
5. 有回归测试和回滚路径；
6. 必要时同步修订 `docs/design/04` 与 `docs/design/05`。

如果 Codex 认为当前设计无法实现、存在明显冲突、严重耦合或明显不符合 AgentScope 实现方式，应记录 Architecture Issue，包括问题描述、涉及模块、当前设计、目标设计、证据、影响范围、建议方案和是否阻塞当前 Task。

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

当前实现中存在：

```text
MainAgent
InterpretationAgent
ValidationAgent
LoggingInterpretationDemoAgent（AgentScope Agent）
```

其中前三者是当前项目职责类／实现资产，不应仅因名称为 Agent 就视为最终多 Agent 架构；真正的 AgentScope Agent 以代码事实为准。

目标方案当前优先验证**一个主 Agent + Prompt / Skill / Toolkit / Context** 能否承担更多理解与决策职责。未经主设计与实验证据，不新增核心业务 Agent，也不提前删除现有职责类。

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

当前 W01-W10 是**完整解释内部的现有确定性业务流程**，用于组织已实现的专业步骤和错误边界。

它不等于：

> 所有用户自然语言操作都必须重新进入 W01-W10。

查询、报告读取、受控修改、版本比较、阶段确认等操作应根据主设计和 Capability 选择合适路径。

Workflow 仍负责当前实现中的步骤编排、状态转换、Retry、Rollback、Review 和 Error Handling，不承担复杂专业推理。

W01-W10 不得在普通 Task 中随意修改；若 AgentScope 原生能力实验表明某些“智能编排职责”可以上移到 Agent / Skill / Toolkit，也必须通过 Architecture Issue、evidence 和迁移 Task 决定，不能直接删除 Workflow 的确定性安全职责。

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

Retry 和 Rollback 必须有明确原因、次数限制、Trace 和状态记录。超过限制后，应进入 `REVIEW_REQUIRED`（需要人工复核）或明确失败状态，禁止无限执行。

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
