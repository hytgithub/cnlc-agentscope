# Task 10.5-E1.1：Execution Bridge Semantic Hardening

## 定位与基线

E1.1 是 E1 执行桥的定点语义加固，只处理只读 PREVIOUS（上一版）锚点、
FULL_INTERPRET（首次解释）的全计划裁决顺序，以及真实 PostgreSQL / Redis 回归。

- 开发分支：`codex/task-10-5-operation-understanding`。
- 开始 HEAD：`0c6f26e6e1449cd35e94d41bfdaaeb7bb5f1baea`。
- 稳定基线：`ba727747c9a8a114364e40ce0650e679b1134bd5`。
- 开始时开发分支相对稳定基线 ahead 8 / behind 0。
- 已有 `.idea/`、两份 `demo_output` JSON 和 `docs/gdsx-tool-function-inventory.md`
  保留且不提交。

## View PREVIOUS 锚点

E1 的 PREVIOUS 只能相对 Active Base 或 Task 当前版本解析，因此用户正在查看 A/V3 时，
即使明确请求 A 的上一版报告，也可能错误地相对 A 的最新版本计算。

`OperationReferenceResolver.resolve_execution` 新增独立的可选查看锚点。
PREVIOUS 按以下顺序选择锚点：

1. `previous_anchor_execution_id`，用于当前查看上下文；
2. `active_base_execution_id`，用于兼容既有 Active Base；
3. `task.current_execution_id`，用于没有上下文锚点的请求。

Bridge 只在 READ Operation（只读操作）、PREVIOUS 且 View 的 task_id 与已授权目标 Task
相同时传入 View execution_id。View 不能跨 Task 使用，也不参与写操作；ACTIVE_BASE
仍只读取 Active Base。来自 View 或 Active Base 的显式上下文锚点都必须存在且属于目标 Task，
失效时返回 STALE_CONTEXT_REFERENCE（上下文引用失效），不得回退到当前版本。
TASK_CURRENT、LATEST_SUCCESSFUL、FIRST、SEQUENCE 和 EXECUTION_ID 行为不变，
写 PREVIOUS 仍受 HISTORICAL_BASE_WRITE_UNSUPPORTED（历史基线写入当前不支持）保护。

验证场景包括：View=A/V3 且 current=A/V4 时返回 A/V2；Active=C 时仍返回 A/V2
并保持 Active C；显式 Task=B 时不借用 A 的 View；无 View 时依次兼容 Active Base 和 Task current。

## FULL_INTERPRET 全计划裁决

E1 曾在发现任一 FULL_INTERPRET 后立即返回 INITIAL_INPUT_ROUTE_REQUIRED
（首次解释需要输入资料入口），可能跳过同一计划中的其他节点、条件或缺槽。

Bridge 现在先执行 PlanValidator 的完整计划裁决。只有结构与语义有效、唯一业务节点为
FULL_INTERPRET 的计划才返回 INITIAL_INPUT_ROUTE_REQUIRED，且不创建 Task、InputVersion 或
Execution。包含 MODIFY_PARAMETER、REPORT 或 FULL_RERUN 的复合计划整体返回
COMPOUND_EXECUTION_UNSUPPORTED（当前执行桥不支持该复合执行）；带条件的计划返回
CONDITIONAL_EXECUTION_UNSUPPORTED（当前不支持条件执行）；缺少 target 的计划进入澄清。
CAPABILITY_QUERY（能力询问）继续返回 READ_ONLY（只读），OUT_OF_DOMAIN（超出领域）继续拒绝。
所有拒绝都发生在命令提交前，保持全计划零副作用原子性。

## 真实持久化回归

本地 Docker PostgreSQL 与 Redis 服务健康。测试进程通过项目 `ConnectionSettings` 读取本地连接，
向测试专用环境变量注入地址后执行真实适配器回归；未输出连接凭据。

新增一个最小 Bridge 持久化集成测试，验证：

- SessionTaskBinding 是 Task 授权来源，Redis/runtime 中的焦点不能授权外部身份；
- STATUS（查询状态）不创建 Execution；
- MODIFY_PARAMETER（修改计算参数）在 PostgreSQL 中持久化一个新 Execution；
- 旧 `expected_current_execution_id` 在真实 Repository 上返回 STALE_EXECUTION_PLAN
  （执行计划已失效），且不新增版本；
- 读取历史报告只更新 Runtime View，不改变业务 Task 当前版本。

真实持久化测试最终结果：

| 检查 | 结果 |
| --- | --- |
| PostgreSQL real persistence | 8 passed |
| SessionTaskBinding | 3 passed |
| Conversation PostgreSQL / Redis | 2 passed |
| Operation Bridge persistence | 1 passed |
| 合计 | 14 passed |

## 完整验证

| 检查 | 结果 |
| --- | --- |
| Reference Resolver + Bridge 定点单测 | 133 passed |
| Parser + Context Resolver + Validator + Bridge 规划回归 | 183 passed |
| `tests/unit/` | 621 passed |
| Interaction robustness + Task ReAct 回归 | 61 passed |
| 真实 PostgreSQL / Redis 回归 | 14 passed |
| Ruff：本次新增/修改 Python 文件 | 通过 |
| mypy：本次修改生产 Python 文件 | 通过 |
| `git diff --check` | 通过 |

## 边界与未实现

本次未修改首次上传稳定链、ReAct、模型调用、系统提示词、Agent Tool Schema、前端、W01-W10、
数据库 Schema 或 migration。没有新增正式状态或错误码，因此未修改状态词汇表。
未实现 E2、COMPARE、SCENARIO、SEGMENT_EDIT、MODIFY_RESULT、RECALCULATE、REINTERPRET、
历史版本分支写或局部重跑。E1.1 未接入页面，不进行人工页面验收。

No Architecture Issue found.
