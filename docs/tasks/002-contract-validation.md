# Task 02：数据与工具契约校验

- 状态：已实现并通过本任务验证。
- 日期：2026-09-20。
- 起始版本：916f725cf26d1a180f8c80751d350abf9c30cc15。
- 依据：Task 01 下一阶段建议、架构第 26/29/79 节及 AGENTS.md。
- 范围：将现有最小契约变成可交换、可校验的技术基线；不冻结未知专业结构。

## 实现和文件

新增：

- `src/cnlc_agent/schema.py`：8 类契约导出、无写入一致性检查、文件校验命令。
- `schemas/*.schema.json`：由运行时 Pydantic 模型生成的 8 份 JSON Schema。
- `docs/04-data-tool-contracts.md`：输入、单位、缺失、结果、工具和未决信息说明。
- `tests/unit/test_schema.py`：导出漂移、跨字段校验、错误输出和退出码。
- 本任务记录。

修改：

- `src/cnlc_agent/tools/contracts.py`：在统一调用边界重新校验返回值，结构错误转换为
  INVALID_TOOL_OUTPUT；校验已构造实例，避免内部容器修改绕过检查。
- `tests/unit/test_contracts.py`：畸形返回值与模型内部容器污染测试。
- `tests/integration/test_workflow.py`：无效工具输出导致 W03 失败、无后续成功结果。
- `README.md`：新增入口和文档链接。

核心设计：Pydantic 是唯一契约来源，静态 Schema 与模型一致性由测试保证。
JSON Schema 无法表达的跨字段约束继续由现有 Pydantic validator 检查。
校验错误不回显井数据，不添加外部依赖。

## 验证

| 检查 | 结果 |
|---|---|
| 起始版本 pytest | 22 passed |
| 完成后 pytest | 33 passed，无警告 |
| ruff check / format --check | 通过 |
| mypy | 30 个源码文件通过 |
| schema export --check | 8 份 Schema 与模型一致 |
| validate mock-fixture | 仓库样例通过 |
| uv build | sdist、wheel 构建成功 |

集成测试覆盖原有完整 W01–W10 与 JSON 往返、缺失数据、工具/模型失败、验证冲突，
并新增工具畸形输出的 Workflow 失败路径。

## 未完成及下一阶段依赖

**Pending final well-data schema.** 本任务不宣称正式专业 Schema、工具专业入参已冻结。
MVP 数据契约、JSON 可读和错误分类能力增强；真实数据库、Redis、模型、AgentScope Runtime、
OpenTelemetry、Web、Retry/Rollback 的验收仍未完成。

下一任务建议为真实 PostgreSQL/Redis 最小持久化：基于现有 Repository / StateStore 接口，
先定义迁移、JSON 状态存储、任务查询、Redis key/TTL 和故障处理，再实施和验证。
内部模型接入仍需模型协议、URL、模型名、鉴权配置；密钥不得提交。
业务 Schema 正式化需项目方提供 04 文档所列的脱敏样例与字段约定。

## Architecture Issue

**No Architecture Issue found.** 未改变业务顺序、核心 Agent 数量、模块职责或基础设施选型。
