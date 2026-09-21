# PostgreSQL / Redis 最小持久化

Task 03 接入真实 SDK 与持久化实现，业务数据、专业工具和模型仍为 Mock。
本次不实现 AgentScope Runtime、真实模型、Web、自动恢复或分布式任务调度。

## 1. 数据边界

| 数据 | 保存位置 | 行为 |
|---|---|---|
| Task ID、Well ID、当前状态和时间 | PostgreSQL interpretation_task | 可按 task_id 查询；well_id/status 有索引 |
| 原始资料、阶段结果、执行记录、状态修改历史 | 同表 snapshot JSONB | 每个节点前后保存完整 InterpretationState |
| 最终/诊断 Markdown | 同表 markdown TEXT | 与最终 JSON 快照同事务提交 |
| 运行时快照 | Redis | 每次 SET 同时设置 TTL；过期可从 PostgreSQL 查询历史 |
| Mock 井资料源 | mock_data 文件 | WellRepository 继续使用 fixture；不是正式井数据平台 |

采用单表最小实现，未冻结专业结果 Schema，因此不提前拆分大量业务表。
Schema 稳定后可通过 Alembic 逐步拆表；不是放弃数据库设计。

## 2. 写入顺序和故障处理

1. 任务服务先在 PostgreSQL 创建 PENDING 任务，主键冲突返回 TASK_EXISTS。
2. Workflow 每个节点执行前后：先提交 PostgreSQL 快照，再写 Redis。
3. 完整流程或业务失败后，任务服务同事务保存最终快照和 Markdown。
4. Redis 写入失败：停止流程，从持久化快照形成 FAILED 诊断并保存到 PostgreSQL。
5. 数据库不可用：明确抛出 InfrastructureError，不返回成功，不降级到内存。

PostgreSQL 与 Redis 之间没有分布式事务。Redis 故障时可能保留旧值，历史查询以 PostgreSQL
为准。中途崩溃可能留下未完成快照；markdown 为空表示报告尚未完成，历史 CLI 返回
REPORT_NOT_READY。当前不自动抢占/恢复任务，不允许重新使用旧 task_id 开始执行。

重复任务 ID 通过数据库唯一约束拒绝，避免两个服务同时执行同一新任务。
不同任务可以使用各自 Session 并行；本阶段不提供任务队列、锁续期和自动故障转移。

Redis key 为 `<CNLC_REDIS_PREFIX>:state:<task_id 的 SHA-256>`，默认 TTL 86400 秒。
SET 使用 ex 原子设置过期，每次保存刷新 TTL；TTL 应长于单个步骤可能的最长运行时间。
数据库长期保存结果，Redis 清空不等于丢失最终报告。当前 get 返回 None 表示缓存缺失。

## 3. 本地运行

需要 Docker Compose、Python 3.11 和 uv。在仓库根目录执行：

```bash
cp .env.example .env
```

编辑 `.env`：

```dotenv
CNLC_PERSISTENCE=postgres-redis
POSTGRES_PASSWORD=自行设置的本地密码
DATABASE_URL=postgresql+asyncpg://cnlc:URL编码后的同一密码@127.0.0.1:5432/cnlc
REDIS_URL=redis://127.0.0.1:6379/0
```

DATABASE_URL 中密码含 `@`、`:`、`/` 等字符时必须做 URL 编码；不把字面示例当真实连接信息。
连接地址不写入源码和 alembic.ini，日志不输出连接串。

```bash
uv sync --locked --dev
docker compose up -d --wait
uv run alembic upgrade head
uv run cnlc-agent --well-id WELL_MOCK_001
```

Compose 的数据库端口仅绑定本机，PostgreSQL 使用具名卷；Redis 为可丢弃缓存。
应用在主机运行，迁移由开发者显式执行，不在启动时自动改表。
可用 POSTGRES_PORT / REDIS_PORT 修改本机端口，并同步修改连接 URL。

输出仍为 JSON + Markdown。业务模式显示 mock，表示解释内容来自 Mock；持久化由
CNLC_PERSISTENCE 独立选择，不要把 mock 误认为数据库必然是内存。

进程重启后查询历史（将 task_id 替换成运行输出的实际值）：

```bash
uv run cnlc-agent --task-id <task_id> --output-dir history
```

使用新的输出目录，避免覆盖已导出的文件。非安全文件名的 task_id 使用散列值作为目录名，
JSON 仍保留原 ID。不存在的任务返回 TASK_NOT_FOUND；未生成报告返回 REPORT_NOT_READY。
失败任务有诊断报告时仍可查询，进程退出码为 1；基础设施/配置错误退出码为 2。

离线演示：设置 `CNLC_PERSISTENCE=memory`。真实模式缺少地址会直接报错，不自动降级。

## 4. 测试

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

真实集成测试需专用测试数据库和 Redis；测试会执行 `alembic upgrade head`，创建随机 ID 的
任务，结束时仅清理自己的任务和缓存键，不清空共享数据库或 Redis。

在本地环境设置 CNLC_TEST_DATABASE_URL、CNLC_TEST_REDIS_URL 为专用测试服务地址后执行：

```bash
uv run pytest tests/integration/test_real_persistence.py -v
```

该测试验证迁移重复执行、真实 Workflow、重复 ID 拒绝、跨进程历史查询、失败报告持久化、
Redis TTL 和缓存损坏。地址缺失会显式 skip；配置了地址但服务不可用则失败。
不能把 skip 当作真实服务验收通过。

首次执行环境没有 Docker/PostgreSQL/Redis 服务，系统安装受权限限制。
2026-09-21 用户在本地运行真实集成测试并提供输出：3 passed in 8.77s。
环境为 macOS / Python 3.11.15 / pytest 9.1.1；已补齐本阶段真实服务验证。
普通测试已覆盖适配器调用、真实客户端连接拒绝、故障分类、资源关闭和流程终止；
迁移 SQL 离线生成通过不等于迁移已在数据库执行。

## 5. 待完成

- 本阶段真实服务测试已通过（用户提供的执行结果）；后续代码变更继续回归验证。
- 后续独立任务接入内部模型与 AgentScope Runtime。
- Retry/Rollback、崩溃恢复、任务锁、Web、OpenTelemetry 仍未实现。
- Pending final well-data schema：正式业务字段和算法需项目方确认。

实现参考官方 [SQLAlchemy asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
和 [redis-py asyncio](https://redis.readthedocs.io/en/stable/examples/asyncio_examples.html)；
连接池按异步上下文关闭，ORM Session 不在多个并发任务之间共享。
