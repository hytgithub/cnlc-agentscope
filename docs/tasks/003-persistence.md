# Task 03：PostgreSQL / Redis 最小持久化

- 日期：2026-09-20。
- 起始版本：3e4057b2ceab40a21435c34643fd3a84ecb93f24（Task 02）。
- 状态：实现完成；本地检查通过，用户提供的真实服务联调结果通过（2026-09-21）。
- 依据：架构第 30–36、58、69–70、73 节，MVP 第 12–13 节及 AGENTS.md。

## 本次实现与核心设计

- Async SQLAlchemy + asyncpg 的 PostgreSQLTaskRepository；任务创建、快照更新、历史查询、报告查询。
- interpretation_task 单表：索引字段 + JSONB 状态 + Markdown；最终报告和状态同事务提交。
- Alembic 0001 迁移及本地 PostgreSQL 16 / Redis 7 Compose；启动不会自动迁移。
- redis-py asyncio StateStore，命名空间、散列任务键、配置 TTL、读写错误分类。
- 应用装配选择 memory 或 postgres-redis，真实模式配置缺失/失败不降级。
- 创建任务唯一约束拒绝重复 ID；节点前后先数据库后 Redis，故障停止并保存诊断。
- 异步资源上下文关闭 Redis 和数据库连接池；CLI 支持 --task-id 历史查询。
- 保留三 Agent、W01–W10、Mock 工具和模型；不增加专业业务规则。

## 新增文件

| 文件 | 用途 |
|---|---|
| infrastructure/database.py | ORM 表与真实 PostgreSQL Repository |
| infrastructure/redis_store.py | Redis 状态适配器 |
| application/checkpoints.py | 持久化快照与运行缓存协调 |
| application/runtime.py | 配置选择与连接生命周期 |
| alembic.ini、migrations/ | 初始迁移、环境、后续迁移模板 |
| compose.yaml | 本地开发数据库与缓存 |
| tests/unit/test_persistence.py | 错误、重复任务、生命周期、客户端连接拒绝等 |
| tests/unit/test_migrations.py | PostgreSQL 迁移 SQL 离线生成 |
| tests/integration/test_real_persistence.py | 可选真实服务验收 |
| docs/05-persistence.md | 持久化设计、运行与验收指南 |
| 本记录 | 任务结果与边界 |

源码相对 src/cnlc_agent；完整差异以 Git 提交为准。

## 修改文件

- application/ports.py：新增 create/get_report。
- application/bootstrap.py：注入 Repository/StateStore。
- application/service.py：任务创建、基础设施故障诊断与持久化。
- agents/main_agent.py：接收任务服务已创建的状态，保持相同 trace_id。
- infrastructure/mock.py：同步最小 Repository 契约。
- config/settings.py、.env.example：持久化模式、连接和 TTL 配置。
- main.py：异步生命周期、历史导出、错误码、输出路径限制。
- tests/integration/test_cli.py：历史输出路径保护。
- pyproject.toml、uv.lock：SQLAlchemy/asyncpg/Alembic/redis 及锁定依赖。
- README.md：当前状态和操作指南。

## 验证结果

| 检查 | 结果 |
|---|---|
| pytest | 49 passed，3 skipped |
| ruff check / format --check | 通过 |
| mypy | 34 个源码文件通过 |
| Schema 与模型一致性 | 通过，未修改既有业务 Schema |
| uv build | sdist 和 wheel 构建通过 |
| git diff --check | 通过 |
| PostgreSQL 迁移实际执行 | 用户本地真实测试 fixture 通过 |
| 真实完整持久化 / 进程重启查询 / Redis TTL | 用户本地 3 passed in 8.77s |

普通测试包括原有正常 W01–W10、缺失数据、模型/工具失败、验证冲突；新增缓存失败后的
持久化诊断、数据库失败阻止缓存写入、重复任务不覆盖、关闭连接、配置和错误脱敏等。
实际 asyncpg/redis 客户端在本机拒绝连接场景的错误分类已通过，但不代表成功连接测试。

执行环境未安装 Docker/PostgreSQL/Redis，系统包安装因 setgroups/seteuid 权限限制失败。
未提供外部数据库连接信息。没有使用 SQLite 或 Fake Redis 冒充真实联调。

## 未完成与下一阶段依赖

1. 真实服务联调已由用户本地执行并回传通过结果，运行方法见 05 文档。
2. 本阶段数据库/Redis 最小能力验收通过；不代表整个 MVP 验收完成。
3. PostgreSQL 保存最后快照，不提供自动恢复；进程中断可能留下未完成任务。
4. 不提供数据库/Redis 分布式事务；缓存故障可能留旧值，以数据库为准。
5. 内部模型、AgentScope Runtime、Web、OpenTelemetry、Retry/Rollback 留给后续任务。
6. Pending final well-data schema；当前持久化存储沿用 0.1-skeleton。

## Architecture Issue

**No Architecture Issue found.** 技术选型、Agent 职责和业务顺序保持既有基线。
首次环境限制已通过用户本地执行补齐验证。

## 2026-09-21 用户本地验收补充

证据来源：用户粘贴的 pytest 运行输出；非助手远程连接用户电脑执行。
环境：darwin、Python 3.11.15、pytest 9.1.1、pytest-asyncio 1.4.0。
命令：`uv run pytest tests/integration/test_real_persistence.py -v`。

| 测试 | 结果 |
|---|---|
| test_real_workflow_restart_and_duplicate | PASSED |
| test_real_cache_expiry_and_corruption | PASSED |
| test_real_failed_workflow_report_is_durable | PASSED |

合计 **3 passed in 8.77s**。覆盖实际迁移、正常任务、重复任务拒绝、跨进程历史查询、
Redis 过期/损坏处理以及失败报告持久化。本次输出未包含 Git SHA 或服务版本，未推断这些信息。
历史 49 passed / 3 skipped 保留为前一次独立验证记录，不表述为本次全量 52 passed。
本次仅更新验收文档与 PR 状态，不修改实现代码，也不保存连接密码。
