# Task 10.4 implementation summary

## 基线与范围

- 日期：2026-09-26。
- 分支：`codex/demo-2026-10-31`。
- 开始 HEAD：`7a445d718ce4894fdf0d4366fe274b5eba44de33`，开始前已 fetch 并确认远端一致。
- 提交名称：`feat: persist conversations with cache retention`；最终 SHA 见任务完成回复。
- 本次只处理 Conversation Session / Message 的长期保存、Redis 活跃缓存生命周期、恢复、删除、
  retention 和对应文档测试。未修改 W01–W10、专业 Tool、company_mock、Heavy API、Skill 或主 Workflow。
- 未修改 main，未创建 PR。

## AgentScope Storage 研究与方案

AgentScope 2.0.8 的 `StorageBase` 同时管理 Session、Message、AgentState、Credential、Agent、Schedule、
Team 等资源。官方 `RedisStorage` 的全局 `key_ttl` 会影响它保存的所有 key；写入会刷新 TTL，读取不会完整
覆盖本 Task 的滑动生命周期。官方 `AsyncSQLAlchemyStorage` 可以把整套 AgentScope 资源写入 SQL，
但会引入 Credential、Agent、Schedule、Team、Knowledge 等上游表，也不提供 Redis 活跃会话缓存。

因此采用 `DurableConversationStorage(RedisStorage)`：保留官方 Contract 和 Redis 实现，只覆盖 Session / Message
生命周期；PostgreSQL durable write 成功后再 best effort 写 Redis。父类全局 TTL 固定为 `None`，再对
Conversation key 选择性 `EXPIRE`。没有修改或 monkey patch AgentScope 源码。

## 实现与核心设计

1. Migration `0007` 新增 `conversation_session` 和 `conversation_message`。Session 以
   `(user_id, agent_id, session_id)` 为 ownership 主键；Message 以 `message_id` 为幂等主键，并用 Session 内
   `sequence` 唯一约束保持排序。
2. PostgreSQL 是 Conversation source of truth。结构化 `Msg` 保留 Text、Thinking、ToolCall、ToolResult、
   DataBlock 的历史 UI 展示能力；其中业务过程、Tool 和报告只是 conversation presentation copy，
   Task / Execution / ToolRun / Execution report 仍是业务 canonical fact。
3. `CNLC_SESSION_CACHE_TTL_SECONDS` 默认 604800 秒，独立于 `CNLC_REDIS_TTL_SECONDS`。
   Session、Message list、用户／Agent Session index，以及 Schedule / Channel Session index 使用滑动 TTL；
   Session 读写和 Message 写入会刷新相关 TTL。
4. Credential、Agent、MCP、Skill、Schedule、Team、Knowledge 等非 Conversation key 不使用 Session TTL。
   真实 Redis 测试确认 qwen-plus Backend Credential 的 TTL 为 `-1`。
5. Redis Session miss 时按完整 ownership 从 PostgreSQL 恢复并回填缓存；Message 历史始终从 PostgreSQL
   游标分页读取。升级前只有 Redis 的旧 Session / Message 会在首次访问时按原顺序归档。
6. PostgreSQL + Redis 不使用分布式事务。PostgreSQL 失败返回稳定 InfrastructureError，不先确认 Redis；
   durable write 成功而 cache 失败时记录安全 warning，后续读取重新回填。
7. 重复 `message_id` 更新原结构化 payload 并保留原 sequence；新消息在父 Session 行锁内分配序号，
   数据库唯一约束提供最终保护。
8. 删除 Session 会删除 durable Conversation 及其 Message，再删除 cache；没有到
   SessionTaskBinding、Task、Execution、ToolRun 或 Report 的级联。
9. `CNLC_CONVERSATION_RETENTION_DAYS` 与 cache TTL 独立；默认 `None` 表示长期保留。配置正整数后，
   Storage 启动时按 `last_active_at` 清理 Conversation，数据库只级联其 Message。
10. durable Session 副本移除 `cnlc_pending_clarification`。Redis 命中时有效的短期 pending 仍保留；
    Redis 丢失或 Backend 重建后不会从旧消息恢复旧参数。
11. `cnlc_active_task_id` 可以随 Session presentation state 恢复；不可用时仍由
    SessionTaskBinding 最近绑定 Task fallback，没有新增业务当前指针。
12. PostgreSQL Message 提供 `before + limit` 分页。100+ 消息测试确认长期历史不会替换
    `AgentState.context`，既有 AgentScope context compression 继续生效。

完整设计见 [../12-conversation-persistence.md](../12-conversation-persistence.md)。

## 文件

新增：

- `migrations/versions/0007_conversation_persistence.py`
- `src/cnlc_agent/infrastructure/conversation.py`
- `tests/unit/test_conversation_persistence.py`
- `tests/integration/test_conversation_storage_integration.py`
- `docs/12-conversation-persistence.md`
- `docs/tasks/010-4-conversation-persistence.md`

修改：

- `.env.example`
- `src/cnlc_agent/config/settings.py`
- `src/cnlc_agent/demo/agentscope_app.py`
- `tests/unit/test_migrations.py`
- `docs/03-system-architecture.md`
- `docs/05-persistence.md`
- `docs/07-database-design.md`
- `docs/08-intent-and-interaction-design.md`
- `docs/README.md`

工作区原有未跟踪 IDE 文件、`demo_output` 和 GDSX 工具清单不属于本提交。

## 自动测试结果

真实持久化测试使用本地 `.env` 指向的 PostgreSQL / Redis，并只把两个测试 URL 传给 pytest；测试业务
provider 固定为 fixture，避免本地 demo / company_mock 运行配置污染测试假设。

| 命令 / 验证 | 结果 |
| --- | --- |
| Conversation + migration 定向测试 | 11 passed |
| `uv run pytest -q tests/unit/` | 225 passed |
| Session binding + Conversation + ReAct + upload 集成测试 | 47 passed |
| `uv run pytest -q` | 376 passed，2 skipped，1 warning |
| `pnpm --filter frontend test` | 5 passed |
| `pnpm --filter frontend build` | PASS，只有既有 chunk size 提示 |
| `pnpm --filter frontend lint` | PASS，0 errors、21 个既有 warnings |
| 本次修改/新增 Python 文件 `ruff check` | PASS |
| 本次修改的 3 个生产 Python 文件 `mypy` | PASS |
| `uv run ruff check .` | FAIL，222 个既有错误 |
| `uv run mypy` | FAIL，243 个既有错误 / 16 文件 |
| `git diff --check` | PASS |

全量 skipped 是现有 opt-in 真实模型网络测试和未配置真实 GDSX 样本测试。warning 是现有
Starlette / anyio 弃用提示。全仓 ruff / mypy 数量与 Task 10.3 已记录基线相同；新增和修改文件的定向
门禁均通过，没有新增 ignore 隐藏问题。

第一次全量测试因 unit / integration 新测试文件同名而发生 pytest import mismatch，已将集成文件改为
唯一名称。另一次把 `.env` 的 demo mode 整体导出给 pytest，造成 22 项既有测试的模式假失败；最终命令
只注入真实测试 URL，376 项全部通过。

## 真实 PostgreSQL、Redis 与页面验收

实际使用 admin 用户、独立会话 `7aca9c8427a942eea099b4167e36891a`：

1. 页面点击“新会话”，通过可见附件入口上传 `WELL_MOCK_001.json`，输入“请解释这口井”。
2. W01–W10 全部成功，页面显示解释过程、standard 五章报告及 Task
   `8d57a8fd-de31-41c5-a842-d9c4f99e3738` 的 Execution #1。
3. PostgreSQL 有 2 条结构化 ConversationMessage；Session、Message、Session index 三个 Redis key 的
   实测 TTL 都约为 604788 秒。
4. 只删除该 Session 的 3 个 Redis Conversation key 后刷新。完整用户消息、W01–W10 过程、五章报告、
   同一 Task ID 与 Execution #1 从 PostgreSQL / Binding 恢复。
5. 恢复后发送“现在执行到哪里了”，页面回答 Execution #1 为 SUCCESS、W01–W10 已完成；PostgreSQL
   Message 增至 4 条，Task ID 不变，Redis 活跃 key 重新具有 TTL。
6. 停止并重新启动 Backend，再刷新同一页面。问题、状态回答、五章报告、Task 与 Execution 全部仍可见，
   页面可以继续交互。

真实集成测试还覆盖：删除 Redis InterpretationState 后 Task/Binding 不丢；重复 message_id 不增加 sequence；
Thinking / ToolCall / ToolResult 结构 round trip；Conversation 删除后 Task 与 Binding 保留；105 条消息分页；
PendingClarification 不恢复；Credential 无 TTL。

## 已知限制与下一阶段依赖

- 当前 Conversation 删除是硬删除，没有回收站或 Archive UI。
- retention 在 Storage 启动时执行，没有独立周期调度器。
- 为兼容 AgentScope 历史 UI，完整 Msg 仍可能复制大型 ToolResult / Markdown 报告展示内容；业务层不会读取
  该副本，后续可在 AgentScope 支持稳定引用 Block 后进一步减小重复。
- 不实现语义记忆、搜索、编辑、分支、Embedding、Vector DB、跨 Session 用户记忆或合规归档平台。
- 全仓既有 ruff / mypy 债务需要独立清理任务。

No Architecture Issue found.
