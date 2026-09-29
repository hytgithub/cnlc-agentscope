# PostgreSQL / Redis 持久化运行设计

Execution、Workflow 和 ToolRun 状态的中文含义见 [11-status-enum-glossary.md](11-status-enum-glossary.md)。

本文说明当前持久化运行边界。表、字段、关系和 migration 历史统一见 [07-database-design.md](07-database-design.md)，这里不重复定义 Schema。

## 1. 基础设施与职责

正式持久模式使用 SQLAlchemy asyncio + asyncpg 访问 PostgreSQL，使用 redis-py asyncio 访问 Redis，使用 Alembic 显式管理 migration。Repository 和 StateStore 隔离 SDK；Agent、Workflow 和专业 Tool 不直接持有数据库连接。

- PostgreSQL 保存 Task、Artifact 元数据、InputVersion、DatasetRevision / DatasetChangeSet、Execution、ToolRun、SessionTaskBinding、报告、执行租约、Conversation Session / Message 及审计事实，是长期 canonical source。GDSX bytes 与曲线 values 明确不进入 PostgreSQL。
- `FilesystemArtifactStore` 第一版在 `CNLC_ARTIFACT_ROOT` 下使用 SHA-256 内容寻址；数据库只保存逻辑 `storage_key`、大小、摘要和归属。相同 bytes 可复用物理对象，但每个 Task 仍创建独立 Artifact 身份，不能跨 Task 授权。
- Redis 保存可丢弃的 `InterpretationState` 运行检查点和有滑动 TTL 的 AgentScope 活跃聊天缓存；服务凭证等非 Conversation 资源不使用 Session TTL。
- 进程内 `observed_task_ids` 和 dispatcher task 仅用于加速与调度，不是业务事实。

Redis 过期或清空不会删除 PostgreSQL 中的版本、报告或任务归属。PostgreSQL 写入失败时必须返回基础设施错误，不能伪造成功，也不能静默降级到 memory。

## 2. 版本和写入路径

JSON 首次上传先校验并规范化输入，创建 Task、InputVersion 和 `QUEUED`（排队等待）Execution，再保存 SessionTaskBinding。GDSX 首次上传在内容存储成功并通过 HDF5/pygdsx 只读检查后，以一个 PostgreSQL 事务创建 Task、`SOURCE_GDSX`（源 GDSX 制品）、artifact-backed InputVersion、Root DatasetRevision 与 `STAGED_CONFIRMATION`（分阶段确认）Execution；若数据库失败，内容寻址对象是无授权 orphan，可由后续清理回收，不会形成可用但不完整的 Task。局部或全量重跑读取 Task 当前指针、最近成功 Execution 和输入摘要，经 DependencyResolver 得到计划，并原子追加新 Execution。历史输入、执行、参数快照、ToolRun 和报告不会被当前版本覆盖。

当前 AgentScope 聊天附件采用 Base64，编码约膨胀 33%。`CNLC_MAX_GDSX_UPLOAD_BYTES` 独立于 5 MiB JSON 限制，默认 50 MiB，并在 Base64 解码前检查编码长度。本路径用于 V0.1 验证；生产大文件应升级为 multipart 或 direct object upload。

AgentScope `ChatService` 会在进入 Agent `reply_stream` / `UploadInterpretationReply`
middleware 之前调用 Conversation Storage 保存用户消息，因此 middleware 无法安全删除已经
durable 的 GDSX DataBlock。正式 `postgres-redis` 模式在路由进入 ChatService 前拒绝 GDSX
Base64 DataBlock，使用 `POST /cnlc/interpretation/artifacts/gdsx` multipart ingress；该入口
直接写 ArtifactStore 并返回无 bytes 的上传收据。随后
`POST /cnlc/interpretation/tasks/gdsx` 只接收 `artifact_id` 和小型业务参数，再原子创建
Task/Artifact metadata/InputVersion/R1/Execution。Conversation PostgreSQL/Redis 不保存 GDSX
Base64。JSON Demo Upload 保持原路径；GDSX Base64 仅保留 memory/test 验证模式。

上述流程明确分为三层：SHA-256 定位的物理 blob 不带 Task 归属；
`GdsxUploadReceipt` 在 Task 尚未创建时只绑定可信的
`(user_id, agent_id, session_id)`，且不包含 `task_id`；消费 receipt 后才在事务中
创建带 `task_id` 的 Artifact metadata。两个会话上传相同 bytes 可共享
`storage_key`，但各自生成独立 receipt/artifact ID，所有权不共享。任务入口
在创建前按完整三元组重新授权，不匹配统一作为不存在。
第一版采用“一个 receipt 只创建一个 Task”：Artifact 主键（内存实现为同锁检查）
是原子消费标识，并发重复请求只有一个成功，其余返回冲突。

`FilesystemArtifactStore.materialize` 在返回临时 Path 前校验实际 size 与 SHA-256；GDSX
解析/公司调用边界还会校验 HDF5 magic 并实际打开文件。失败统一阻断为
`ARTIFACT_INTEGRITY_FAILED`（制品完整性失败）或 `GDSX_FILE_INVALID`（GDSX 文件无效）。
内容寻址 blob 可被多个 Task 的独立 Artifact metadata 引用，因此数据库事务失败时绝不
unlink blob；无 metadata 的 orphan blob 暂时保留，定期 orphan GC 属于后续维护任务。

Workflow 检查点先写 PostgreSQL，再尝试写 Redis。Redis 写失败会被分类并使流程形成明确失败事实；数据库检查点失败时不写缓存，避免 Redis 出现比 durable fact 更新的假状态。PostgreSQL 与 Redis 没有分布式事务，读取业务历史始终以 PostgreSQL 为准。

报告与产生它的 Execution 同版本保存到 `interpretation_execution.markdown`。Task 的状态、快照和 Markdown 是兼容当前视图。报告尚未形成时不会从聊天文本或缓存拼装。

## 3. 事务、并发和锁

同一 Task 创建新 Execution 时，Repository 在数据库事务内以 `SELECT FOR UPDATE` 锁 Task，校验 `expected_current_execution_id`，并检查当前 Execution 是否活跃。旧计划被拒绝，活跃冲突返回 `TASK_EXECUTION_ACTIVE`；成功后在同一临界区分配连续 sequence 并更新当前指针。

Task、InputVersion、Execution、ToolRun 和 Binding 的写入均依赖数据库约束。Repository 将可预期的唯一约束、归属和并发冲突转换为稳定错误码，不向浏览器暴露驱动异常或连接信息。AsyncSession 不跨并发任务共享。

解编后局部曲线修改采用 metadata-only DatasetRevision 与稀疏 DatasetChangeSet。Root 只引用
InputVersion，Child 与 ChangeSet 在一个事务中追加，不复制完整 RawData，也不移动 Execution
指针。物化和分支规则见 [Dataset Revision & ChangeSet](architecture/dataset-revision-change-set.md)。

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

Alembic migration `0001`～`0011` 必须由部署或开发者显式执行；应用启动不自动改表。`0011` 增加 Artifact metadata 及 artifact-backed InputVersion，旧行按 `FIXTURE`（夹具正文）兼容。迁移顺序和含义见数据库设计。内存模式只用于离线 Demo 和单元测试，不能冒充跨进程持久化。

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
