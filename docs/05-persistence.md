# PostgreSQL / Redis 持久化运行设计

Execution、Workflow 和 ToolRun 状态的中文含义见 [11-status-enum-glossary.md](11-status-enum-glossary.md)。

本文说明当前持久化运行边界。表、字段、关系和 migration 历史统一见 [07-database-design.md](07-database-design.md)，这里不重复定义 Schema。

## 1. 基础设施与职责

正式持久模式使用 SQLAlchemy asyncio + asyncpg 访问 PostgreSQL，使用 redis-py asyncio 访问 Redis，使用 Alembic 显式管理 migration。Repository 和 StateStore 隔离 SDK；Agent、Workflow 和专业 Tool 不直接持有数据库连接。

- PostgreSQL 保存 Task、InputVersion、Execution、ToolRun、SessionTaskBinding、报告、执行租约、Conversation Session / Message 及审计事实，是长期 canonical source。
- Redis 保存可丢弃的 `InterpretationState` 运行检查点和有滑动 TTL 的 AgentScope 活跃聊天缓存；服务凭证等非 Conversation 资源不使用 Session TTL。
- 进程内 `observed_task_ids` 和 dispatcher task 仅用于加速与调度，不是业务事实。

Redis 过期或清空不会删除 PostgreSQL 中的版本、报告或任务归属。PostgreSQL 写入失败时必须返回基础设施错误，不能伪造成功，也不能静默降级到 memory。

## 2. 版本和写入路径

首次上传先校验并规范化输入，创建 Task、InputVersion 和 `QUEUED`（排队等待）Execution，再保存 SessionTaskBinding。局部或全量重跑读取 Task 当前指针、最近成功 Execution 和输入摘要，经 DependencyResolver 得到计划，并原子追加新 Execution。历史输入、执行、参数快照、ToolRun 和报告不会被当前版本覆盖。

Workflow 检查点先写 PostgreSQL，再尝试写 Redis。Redis 写失败会被分类并使流程形成明确失败事实；数据库检查点失败时不写缓存，避免 Redis 出现比 durable fact 更新的假状态。PostgreSQL 与 Redis 没有分布式事务，读取业务历史始终以 PostgreSQL 为准。

报告与产生它的 Execution 同版本保存到 `interpretation_execution.markdown`。Task 的状态、快照和 Markdown 是兼容当前视图。报告尚未形成时不会从聊天文本或缓存拼装。

## 3. 事务、并发和锁

同一 Task 创建新 Execution 时，Repository 在数据库事务内以 `SELECT FOR UPDATE` 锁 Task，校验 `expected_current_execution_id`，并检查当前 Execution 是否活跃。旧计划被拒绝，活跃冲突返回 `TASK_EXECUTION_ACTIVE`；成功后在同一临界区分配连续 sequence 并更新当前指针。

Task、InputVersion、Execution、ToolRun 和 Binding 的写入均依赖数据库约束。Repository 将可预期的唯一约束、归属和并发冲突转换为稳定错误码，不向浏览器暴露驱动异常或连接信息。AsyncSession 不跨并发任务共享。

## 4. 后台执行、租约和崩溃恢复

任务级 Tool 只创建 `QUEUED`（排队等待）Execution 并提交到 `InProcessExecutionDispatcher`。Worker 原子 claim 后进入 `RUNNING`（正在执行），记录 `lease_owner`、`lease_expires_at` 和 `started_at`；运行期间按租约周期约三分之一续租。终态写入需匹配当前 Worker、有效租约和 Task 当前指针，然后写 `finished_at`、清租约并保存最终报告。

服务启动和运行期间会扫描过期 `RUNNING`（正在执行）。扫描使用行锁和 `SKIP LOCKED`，只把失联执行标为 `FAILED`（执行失败） 并写安全错误码，不自动重放可能有外部副作用的 Workflow。应用正常退出会取消并等待本进程 Worker，让 Worker 尝试形成明确终态。

当前没有分布式任务队列，也没有跨进程接管未完成 Workflow。可恢复的是持久事实、任务归属、历史状态和报告；不是从任意 Python 调用栈断点继续。

## 5. Session Binding 与 Web 恢复

PostgreSQL 的 `(user_id, agent_id, session_id, task_id)` Binding 是业务 ownership。Backend 重启后，SessionTaskToolFactory 可从 Binding 恢复 runner 的任务集合；Read API 再按 Task 和 Execution 查询面板、历史、ToolRun 和报告。错误用户、Agent、Session 或 Execution 归属统一拒绝。

AgentScope Session / Message 长期副本由 PostgreSQL Conversation 表保存；Redis 命中时直接使用活跃缓存，缓存过期或清空时按完整 ownership 恢复 Session，并由 Message 表分页恢复历史。业务 runner 仍从 SessionTaskBinding 恢复，业务读模型仍从 Task / Execution 构造。SSE 断开不会取消受 `shield` 保护的后台 Worker；当前没有 SSE replay。完整边界见 [12-conversation-persistence.md](12-conversation-persistence.md)。

## 6. Redis 缓存

运行快照 Key 为 `<CNLC_REDIS_PREFIX>:state:<task_id SHA-256>`，避免在 Key 中暴露原始 ID。SET 原子附带 TTL，每次保存刷新过期时间。缺失返回 `None`；损坏快照和网络错误分类为稳定 `InfrastructureError`，不吞掉异常。

运行快照 TTL 使用 `CNLC_REDIS_TTL_SECONDS`；聊天缓存另用 `CNLC_SESSION_CACHE_TTL_SECONDS`，只作用于 Session、Message 和对应 Session index，并随活跃读写刷新。Credential 等 key 不设置聊天 TTL。长期 Conversation retention 由独立的 `CNLC_CONVERSATION_RETENTION_DAYS` 表达，默认不清理。Redis 数据可以丢失；PostgreSQL 事实不依赖缓存存活。

## 7. 配置与 migration

正式模式设置 `CNLC_PERSISTENCE=postgres-redis`，并提供 `DATABASE_URL` 与 `REDIS_URL`。数据库连接必须使用 `postgresql+asyncpg`。密码、Token 和 API Key 只从未纳入版本控制的环境配置读取，使用 SecretStr 包装，日志不得输出连接串、驱动原文或密钥。

Alembic migration `0001`～`0007` 必须由部署或开发者显式执行；应用启动不自动改表。迁移顺序和含义见数据库设计。内存模式只用于离线 Demo 和单元测试，不能冒充跨进程持久化。

示例：

```bash
uv sync --locked --dev
docker compose up -d --wait
uv run alembic upgrade head
uv run python -m cnlc_agent.demo.agentscope_app
```

## 8. 故障语义

- 数据库不可用或事务失败：明确失败，不写 Redis 假成功。
- Redis 写入失败：保留 PostgreSQL 已写事实，并按 Workflow 错误边界形成失败状态。
- Worker claim 前异常：尝试 claim 后以 `BACKGROUND_EXECUTION_FAILED` 终结。
- Worker 失联：租约过期恢复为 `FAILED`（执行失败），不自动重跑。
- 报告失败：Execution 保留状态和诊断；后续新版本可只运行报告阶段，但不会覆盖旧版本。
- Binding 保存失败：首次任务不向会话宣称可用，返回稳定 `SESSION_TASK_BINDING_FAILED`。
- Conversation PostgreSQL 写失败：不先写 Redis，不向用户确认不可恢复的历史。
- Conversation durable 写成功而缓存失败：保留 PostgreSQL 事实并记录安全 warning，后续读取回填缓存。

所有异常至少包含分类、日志、Trace 和是否影响执行的明确结果。日志只记录安全错误码和异常类型。

## 9. 测试与边界

单元测试覆盖缓存、版本序列、并发创建、claim/heartbeat/finish、租约过期、故障分类和资源关闭。真实集成测试在专用 PostgreSQL/Redis 上覆盖 migration、InputVersion/Override 跨 Repository 恢复、原子 claim、过期回收、Session Binding 重启恢复和 Read API。

当前仍是 in-process dispatcher；分布式队列、自动副作用重放、Heavy Prediction API、独立 Report Artifact 生命周期和最终井数据 Schema 均属于 Future。Pending final well-data schema。
