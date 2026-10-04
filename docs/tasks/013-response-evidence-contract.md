# Task 013｜Response Evidence Contract（最终回复证据合同）

## 1. 目标与边界

在 Task 012E 确认的职责边界上实现最小生产回复合同：把 Application 实际结果转换为不可变 `ResponseEvidenceEnvelope`，再由服务器 renderer 决定哪些业务事实可以对用户表达。它位于执行结果之后，不是新的 OperationPlan，不新增业务状态机。

本 Task 不改 W01～W10、专业计算、OperationPlan、Resolver、Validator/Policy、Bridge 或 StageOrchestrator；不接公司真实 API，不要求 qwen-plus。

## 2. 回复链路现状（代码核验）

核验入口和实现：

- Agent / Web 入口：`src/cnlc_agent/demo/demo_agent.py` 的 `LoggingInterpretationDemoAgent` 注册 `InteractionStateMiddleware`、`UploadInterpretationReply`、`ExecutionStreamingMiddleware`。
- 模型后续操作 Tool：`src/cnlc_agent/demo/operation_tool.py` 输出 `OperationToolResult`；`OperationBridgeResult.outcome=SUCCESS` 仅表示命令提交成功或只读命令返回，不等同后台解释完成。
- 命令结果：`src/cnlc_agent/application/commands.py` 的 `TaskCommandResult.execution_status` 才是 Execution 当前真实状态；`TaskCommands.project()` 从 Repository 重新读取 Execution、报告、InputVersion 和 ToolRun。
- 提交与确认：`src/cnlc_agent/demo/task_tools.py` 的 `TaskCommandRunner` 负责 Task 命令，确认阶段从 StageOrchestrator 找到当前等待的 stage / stage_run 后确认；非报告阶段提交后续 Execution。
- 聊天中间件：`InteractionStateMiddleware` 对错误、澄清、状态和报告读结果生成固定回复；此前对创建型结果通常继续交给 `ExecutionStreamingMiddleware`。后者按本轮 `task_id + execution_id` 等待真实执行终态并读取同一 Execution 的持久报告。
- 上传链：`UploadInterpretationReply` 将附件映射到 START，之后复用同一执行流。解析失败由服务端输出错误消息。
- 前端：AgentScope reply events / SSE 将中间件正文显示在 Web chat；右侧 Panel 和只读 HTTP API 读取独立结构化视图，不是 LLM 自由文本回复。

此前的风险点是：ToolResult 即使成功，也可能只代表任务入队；对没有业务 ToolResult 的自然语言最终文本没有统一证据合同门控，模型可能在事实缺失时自行补充查询/修改成功语义。Mock / Fixture 与真实来源也不能只靠隐藏 metadata 区分。

## 3. ResponseEvidenceEnvelope

合同定义位于 `src/cnlc_agent/domain/response_evidence.py`，继承严格 Contract 并设置 `frozen=True`。字段为：

| 字段 | 用途 |
| --- | --- |
| `response_id` | 单次回复证据 ID |
| `task_id`, `well_id`, `well_name` | 任务和井身份 |
| `execution_id` | 实际 Execution 身份 |
| `scope` | 已解析 scope 类型、权威引用、说明及适用的深度边界 |
| `input_version_id`, `revision` | 输入版本与 Execution 版本序号 |
| `operation` | 查询、启动、修改、重跑、状态、报告读取/生成、阶段确认或其他交互 |
| `status`, `execution_status` | 回复门控状态与原始执行状态分开保存 |
| `source_type` | `REAL` / `MOCK` / `FIXTURE` / `DERIVED` / `MIXED` / `UNKNOWN` |
| `evidence_refs` | Task、Execution、InputVersion、ToolRun、StageRun、Report 或 Interaction 的引用 ID 和来源 |
| `result_summary`, `error_code`, `clarification` | 结果摘要、稳定错误码或必要澄清 |
| `report_markdown` | 仅为指定 Execution 已持久化报告的原样正文 |
| `confirmed_stage`, `confirmed_stage_run_id` | 仅在阶段确认时绑定实际确认事实 |
| `created_at` | 服务端合同生成时间 |

`TaskCommandResult` 包含该 envelope 及来源和证据 refs；Operation 交互结果使用 ScopeResolver 给出的已绑定范围覆盖结果内的默认 whole-well 投影。成功合同要求匹配 Task / Execution 证据引用；报告读取、报告生成和报告阶段确认成功还要求 Report 引用。无 Application 结果的工厂拒绝生成 `SUCCESS`。

## 4. 状态门控和渲染

回复状态为：`SUCCESS`（成功）、`QUEUED`（排队等待）、`RUNNING`（正在执行）、`NEED_CLARIFICATION`（需要澄清）、`UNSUPPORTED`（当前不支持）、`REJECTED`（请求已拒绝）、`FAILED`（执行失败）。这是回复语义门控，不替代 `ExecutionStatus`，其中 `WAITING_CONFIRMATION`（等待阶段确认）仍保存在原 Execution 状态机。

`src/cnlc_agent/demo/response_renderer.py` 提供确定性 renderer：

- 成功回复的 Task、Execution、scope、revision 取自 envelope；报告正文只使用本轮指定 Execution 的原样持久内容。
- 修改 / 全量重跑只有真实终态 `SUCCESS` / `WARNING` 才使用已完成语义；仅入队或运行中只说排队 / 进行中。
- 澄清只提出澄清问题；不支持和拒绝明确表示未执行；失败显示错误和下一步。
- `REPORT_READ`（读取已有报告）与 `REPORT_GENERATION`（生成新报告）使用不同 `operation`。
- 阶段确认单独显示“确认已记录”；除非后续 Execution 自己到达终态，否则不称下一阶段已完成。最终报告阶段确认同时有 Report evidence 才展示报告正文。
- `FIXTURE`、`MOCK`、`MIXED` 结果正文出现“非真实业务结果”；`UNKNOWN` 有 evidence 时标“结果来源未核验”；`DERIVED` 标明是派生结果并保留上游引用。
- 没有 Tool / Application 结果时，middleware 替换模型自由文本，明确本轮没有查询或修改，并同步替换 Assistant 上下文中的自由文本块；ToolCall / ToolResult 审计块保留。业务安全不依赖 regex。

## 5. 接入范围及尚未覆盖边界

已接入 AgentScope Tool / operation、上传提交、状态 / 报告读取、阶段确认与 execution streaming 的聊天回复。流式执行末尾从 `TaskCommandResult` 构造 envelope；最终报告仍为指定 Execution 的原文，不经过 LLM 重写。`TaskCommandResult` 响应序列化时也携带合同，便于现有业务读接口调用方审计来源和身份。

尚未把 `InterpretationExecutionView` / `InterpretationTaskView`、独立 Panel/Read API 和 `GdsxTaskCreateResponse` 改成统一 envelope 响应。它们当前为有类型的结构化 DTO，不经过模型生成自然语言；后续 API 契约迁移应单独评审，不能宣称本 Task 已覆盖所有 HTTP 输出。上传格式校验错误没有业务执行结果，聊天只返回无成功语义的安全说明。

`OperationToolResult.outcome=SUCCESS` 仍保留原业务含义——命令提交/读取层成功。不得据此移除 Operation、Bridge、Validator 或 StageOrchestrator。

## 6. Task 012A.1 测试污染调查

此前同一 pytest 进程先收集/导入生产测试后，实验测试 `test_poc_import_does_not_load_production_task_runner` 会因 `cnlc_agent.demo.task_tools` 已存在于 `sys.modules` 而失败；POC 源码本身无生产 import。根因是测试依赖 pytest 进程全局 `sys.modules` 的导入顺序状态，不是 global singleton、环境变量、AgentScope state 或共享 fixture，也不是生产行为错误。

该测试现在在独立 Python 子进程中导入 POC agent 并检查 production task runner 没有被带入。这样检查仍验证 POC import 边界，但不受同一个 pytest invocation 收集顺序影响；没有改生产代码来掩盖隔离问题。

## 7. 测试与验收

新增 `tests/unit/test_response_evidence.py` 覆盖十项契约：无结果、澄清、不支持、拒绝、RUNNING、标识/范围/版本一致、Fixture/Mock 非真实标识、修改无执行结果、报告读取与生成差异、阶段确认与下一阶段完成差异；另检查成功合同证据约束。

`tests/unit/test_response_middleware.py` 证明没有 Tool/Application result 时，模型“已经查询”及专业数值最终文本会被安全 renderer 替换。实验隔离测试切换为子进程导入验证。

验收结果：

- 同一 pytest 进程运行 `.venv/bin/pytest tests/experiments tests/unit tests/integration/test_demo_web_upload.py tests/integration/test_interaction_robustness.py tests/integration/test_task_react.py tests/integration/test_demo_report_e2e.py tests/integration/test_stage_progress_api.py -q`：`870 passed`。
- 本 Task 修改的 Python 源码和测试文件通过 `.venv/bin/ruff check <修改文件>`。
- `git diff --check` 通过。
- 更宽的 `pytest tests/experiments tests/unit tests/integration -q`：`949 passed, 22 skipped, 3 failed`。失败为两个 CLI 场景和 `test_official_chat_upload_sse_and_saved_report`。CLI 输出 `ValidationError`；上传用例收到失败 ToolResult，现有工具日志也为 `ValidationError`，其失败消息缺少测试原先预期的 `metadata.result`。前序诊断曾观察到公司 token 字段校验错误，但单独注入 `CNLC_COMPANY_TOKEN=offline-test-token` 未消除全部失败，因此完整根因仍需在正确配置环境复核。失败不在 ResponseEvidence 契约断言中；这三个端到端配置场景未通过，不能计入验收通过数。

本 Task 不运行 qwen-plus 或真实公司 API。失败详情和依赖配置需在提供有效、离线测试配置的环境中复验；不以接通真实公司 API 规避失败。

## 8. Documentation Impact

| 文档 | 影响 |
| --- | --- |
| `docs/02-agent-tool-boundary.md` | 增加最终回复的证据合同与模型自由文本边界 |
| `docs/03-system-architecture.md` | 增加 TaskCommandResult → envelope → gate → renderer 链路和未覆盖 API 边界 |
| `docs/08-intent-and-interaction-design.md` | 增加响应状态与来源标记语义 |
| `docs/09-streaming-progress-and-ui-design.md` | 说明流式正文 Grounding、报告和阶段确认语义 |
| `docs/11-status-enum-glossary.md` | 新增 ResponseStatus、ResponseOperation、ResponseSourceType、ResponseEvidenceKind 中文词典 |
| `docs/design/04-测井解释智能体技术方案.md` | 把 012E Grounding 决策补为首阶段实现和剩余边界 |
| `docs/design/05-测井解释智能体测试与验收方案.md` | 增加 Task 013 离线验收场景 |
| `docs/README.md` | 导航到 Task 013 及当前回复合同 |

## 9. 下一阶段依赖

建议下一最小任务先盘点 `InterpretationExecutionView` / `InterpretationTaskView`、Panel 和 GDSX task-create HTTP DTO 的消费方，再决定是否以版本化 API 方式加入 envelope；保持向后兼容并做前端页面验收。不得把 Task 013 聊天 renderer 改成第二套执行状态机，也不得引入 LLM Grounding regex。
