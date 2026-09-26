# 数据与工具契约：当前可执行基线

状态与枚举中文说明见 [11-status-enum-glossary.md](11-status-enum-glossary.md)。

本文件描述已实现的 `0.1-skeleton` 契约，不冻结正式专业数据格式。
**Pending final well-data schema.** 项目方提供真实脱敏样例后再修订字段、单位及业务规则。
不改变三个 Agent、W01–W10 顺序和基础设施选型。

## 1. 契约来源与校验

Python Pydantic 模型为唯一生成来源；`schemas/*.schema.json` 为可交付的 JSON Schema
Draft 2020-12 文件。禁止单独修改生成文件。

```bash
uv run python -m cnlc_agent.schema export
uv run python -m cnlc_agent.schema export --check
uv run python -m cnlc_agent.schema validate mock-fixture mock_data/WELL_MOCK_001.json
uv run python -m cnlc_agent.schema validate interpretation-state outputs/<task_id>/result.json
```

退出码：0 成功，1 数据不合法或 Schema 与代码不一致，2 文件读写失败或命令参数错误。
校验失败只显示字段路径和错误类型，不回显输入数据。该命令不启动 Workflow、不调用模型。

| 契约名 | 模型 | 用途 |
|---|---|---|
| task-request | TaskRequest | 创建任务：task_id、well_id、instruction |
| well-data | WellData | 井信息、原始数据、演示数据要求；不包含预设结果 |
| mock-fixture | MockFixture | WellData + 显式 Mock 输出及验证结果 |
| stage-result | StageResult | 阶段结果、证据、冲突、缺失、警告、来源 |
| validation-result | ValidationResult | 阶段结果 + 验证状态、回退建议 |
| interpretation-state | InterpretationState | 输入、阶段结果、任务状态与修改历史 |
| tool-input | ToolInput | task_id、trace_id、well_id、step_id、parameters |
| tool-output | ToolOutput | status、data、warnings、errors、metadata |

JSON Schema 可检查字段与类型；深度递增、采样数量对齐、Mock 标记等跨字段规则由
Pydantic validator 检查。因此外部 JSON Schema 校验通过后，仍需运行上述 validate 命令。
此检查只验证数据契约，不代表资料足够解释或专业结论正确。

## 2. 输入与缺失约定

- `well.well_id` 使用字母、数字、下划线、连字符，首字符为字母或数字，最长 64 字符。
- `raw_data.depths` 严格递增；当前仅支持 `m` 和 `MD`。
- 每条曲线必须声明非空 `unit`，`values` 与 depths 等长。
- 单个缺失采样为 JSON `null`；不能用 NaN/Infinity 替代数值。缺失曲线省略键。
- 不自动把 -999 等数值转成缺失值；导入映射需由数据提供方明确。
- 不自动转换单位，不推断 CNL/Sw 等参数使用百分数还是小数。
- `requirements.required_curves` 和 `recommended_sources` 是当前演示输入声明，
  不是正式区块解释标准。Required 的可用采样检查由 W02 执行。
- `auxiliary` 承载辅助资料；自定义输入字段放入 Well/RawData 的 `extensions`。
- 未声明的顶层字段被拒绝，避免拼写错误被静默忽略。

## 3. 结果与 State

StageResult 保留 result、evidence、conflicts、missing_evidence、warnings、recommended_action，
必须声明 is_mock 与 source。result 目前仍为 JSON 对象，具体专业参数及层段 Schema 未冻结。
ValidationResult 的 validation_status 与步骤运行 status 是不同概念：前者由 Workflow
解释成继续、警告或人工复核，不能直接覆盖专业解释结果。

InterpretationState 由 Workflow 修改，保留执行记录、修改前后值、修改者、原因和时间。
当前版本仅支持 mock；retry_count、rollback_count 为预留字段，不能据此声称已支持重试恢复。
后续持久化可使用这套序列化结构，但需单独确定表结构、状态键、TTL 与迁移策略。

## 4. 当前工具契约

所有工具使用 ToolInput / ToolOutput 信封；当前 parameters 尚无专业入参定义。

| Tool | 步骤 | 职责 | 成功 data 类型 | 当前实现 |
|---|---|---|---|---|
| get_well_data | W01 | 读取资料 | WellData | GetWellDataTool + MockWellRepository |
| check_curve_quality | W03 | 返回 QC 结果 | StageResult | MockResultTool / qc |
| identify_lithology | W04 | 返回岩性结果 | StageResult | MockResultTool / lithology |
| evaluate_petrophysics | W05 | 返回物性结果 | StageResult | MockResultTool / petrophysics |
| calculate_sw | W06 | 返回 Sw 计算结果 | StageResult | MockResultTool / sw |
| merge_intervals | W08 | 返回层段结果 | StageResult | MockResultTool / intervals |

统一 ToolCaller 重新校验返回信封，包括已构造但内部容器被修改的模型实例。
成功信封只接受 `SUCCESS`（执行成功）/`WARNING`（完成但有告警），且 errors 为空；data 的业务结构继续由消费节点验证。
这不是 Agent 的正式工具权限矩阵，后续 Runtime 接入时另行定义。

| 错误 | 含义 | 当前行为 |
|---|---|---|
| TOOL_TIMEOUT | 超过配置的调用时间 | 标记可重试，当前 Workflow 仍终止 |
| INVALID_TOOL_OUTPUT | 返回值不符合信封 Schema | 当前节点 `FAILED`（执行失败），无后续成功结果 |
| TOOL_FAILED | 未分类工具执行异常 | 当前节点 `FAILED`（执行失败） |
| TOOL_REPORTED_FAILURE | 非成功状态或带 errors | 当前节点 `FAILED`（执行失败） |
| WELL_NOT_FOUND / INVALID_FIXTURE | 数据读取或结构失败 | 保留原 ApplicationError 分类 |
| MOCK_TOOL_RESULT_MISSING | 缺少预设工具结果 | 当前节点 `FAILED`（执行失败） |

超时由 `CNLC_TOOL_TIMEOUT_SECONDS` 控制，默认 10 秒。
Task 02 不新增 Retry、Rollback 或专业计算公式。

## 5. 正式化所需信息

1. 一口完整脱敏测试井和曲线命名映射。
2. 深度类型、采样间隔、参数单位、缺失值编码。
3. 每一步 Required/Recommended/Optional 数据表。
4. 阶段参数、层段、证据引用的业务结构与分类字典。
5. 专业算法接口、阈值和适用区块，不能由 Codex 猜测。

在这些信息确认前，现有 Schema 可支持 Mock 联调和技术持久化开发，不能作为正式井数据标准。
