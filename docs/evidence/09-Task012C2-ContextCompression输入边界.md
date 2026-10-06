# Task 012C.2｜Context Compression 当前输入与历史摘要边界

> 日期：2026-10-06
> 分支：`codex/task-12-agentscope-native-capability-poc`
> AgentScope：2.0.8（项目锁定版本）
> 性质：代码级调用链核验与生产回归证据；不是 AgentScope 全面能力验收

## 结论

Conversation Context 对压缩后当前输入边界的决策为 `WRAP`（包装）：AgentScope 2.0.8 没有在模型消息结构中提供可直接依赖的、语义明确的“当前用户轮次”与“历史摘要”独立字段。项目使用 `on_reply` 收到的原始 `inputs`，在当前 reply 生命周期内保留本轮用户消息，并由 `on_model_call` 在模型调用前检查、恢复被压缩丢弃或切分的当前消息。reply 结束即清除该暂存值，不将它写入 Conversation 持久化或业务 Authority。

`Historical Summary` 只帮助语言理解。WELL / Task / Execution / Scope / Version 与写权限仍由当前用户输入的显式证据约束，并最终由 Session Binding、Resolver、Repository、Execution 和既有写入校验确定。未发现 summary 成为 Authority 的路径。

## AgentScope 2.0.8 调用链核验

复核安装包源码 `agentscope/agent/_agent.py`：

1. `reply_stream()` 的 reply middleware 收到原始 `input_kwargs`，其中 `inputs` 是本轮传入的 `Msg` 或消息列表；随后进入 Agent `_reply_impl` 与 `_handle_incoming_messages`。
2. 每轮 Reasoning 前 Agent 调用 `compress_context()`；上下文过阈值时先生成 summary，再更新 `state.summary` 并以保留消息集合替换 `state.context`。
3. 压缩分割逻辑可能拆分边界消息内容；被拆分或未保留的当前 UserMsg 不保证仍完整存在于随后模型输入中。
4. `_prepare_model_input()` 把 summary 作为 `UserMsg(name="user", content=state.summary)` 加入消息序列；模型侧不能仅凭 user role 将该消息识别为原始当前输入。
5. `on_model_call` 收到即将传给模型的 `messages`。检查当前消息 ID；若缺失或被裁剪，则从本 reply 暂存的原始输入恢复到上下文及模型消息尾部。
6. middleware `on_reply` 的原始 `inputs` 是本项目可用的当前轮来源边界；AgentScope 未提供能替代该生命周期包装的专用语义字段。

## C01–C08 回归结果

| 场景 | 结果 | 证据要点 |
|---|---|---|
| C01 压缩后完成 Pending Clarification | 通过 | 模型收到的末条用户 instruction 精确为“孔隙度”；原 Task 只创建一次新 Execution；Pending 消费后清除；重复请求不再执行。ContextVar 在 reply 返回后为空。 |
| C02 摘要含旧井，查当前报告 | 通过 | 压缩摘要同时包含 WELL_MOCK_001 / WELL_MOCK_002；“给我报告”仍绑定 active WELL_MOCK_002。 |
| C03 本轮显式井号优先 | 通过 | 本轮明确 WELL_MOCK_001 时由 Resolver 返回对应 Task；摘要旧井没有覆盖显式引用。 |
| C04 “上一口井” | 通过 | summary 可保留语言背景；PREVIOUS_TASK 候选必须有本轮“上一口井”等措辞，实际目标按 Session 顺序解析。 |
| C05 摘要不能赋予写范围 | 通过 | 当前仅说参数修改时返回 `CLARIFICATION_REQUIRED`，没有新 Execution；摘要中的“全井”及注入的 WHOLE_WELL 候选不能满足本轮明示校验。 |
| C06 本轮明确整井 | 通过 | 当前用户明确写出“全井”时执行；有效参数和 Task 仍受既有写边界约束。 |
| C07 摘要旧版本、Authority 已推进 | 通过 | summary 保持旧 Execution；Authority 创建更新版本后，“看当前报告”返回最新 Execution。 |
| C08 压缩后只读层段指代 | 安全澄清 | summary 提供的第 5 层不能单独选层；本轮未明确范围且 View 未建立时返回澄清，不执行查询。 |

## 独立指标

| 指标 | 结果 |
|---|---|
| Current-Turn Fidelity | 8/8 场景的边界结果符合预期；C01–C07 直接断言记录到的模型 instruction 为本轮输入，C08 断言 summary-only 层段候选被拒绝且没有查询。 |
| Summary Leakage | 8 个场景未观察到 summary 单独成为本轮 WELL / Task / Execution / Scope / Version 引用或写授权。 |
| Authority Binding | CURRENT、显式井号、PREVIOUS_TASK 和最新版本均由已有 Session / Resolver / Repository 事实解析；summary 不提供权威 ID。 |
| Write Safety | 0 次观察到 summary 单独导致写入授权或新 Execution；C05 及 summary-only WholeWell 对抗测试均澄清。 |
| Pending Safety | C01 通过：原 owner / 生命周期路径消费一次，Pending 清除；重放不产生第二次 Execution。 |
| Grounding | 请求及报告结果仍来自 ToolResult / Repository；summary 不被用作报告内容。C08 缺少可信范围时不编造查询结果。 |

这些是当前 Mock / fixture 集成回归中的场景结果，不是模型总体准确率、真实生产 Session 验收或真实测井数据验收。Write Safety 单独报告：本轮观察到的失败数为 **0**。

## 变更与安全边界

- `InteractionStateMiddleware` 将当前原始 UserMsg 放入 request-local `ContextVar`；异常退出也保证清理，不跨 reply 残留。
- 模型调用前按消息 ID 恢复被压缩丢弃或分割的当前 UserMsg。
- WholeWell、显式 Well / Task / Execution / 版本及层段序号的本轮证据校验，不再反向扫描可能包含 summary 的 AgentState context。
- 缺当前轮证据时澄清；Resolver / Repository 与 Task 012C.1 的确定性写范围边界不变。
- 没有新增数据库、Redis key、Conversation 持久化字段或业务 Authority 字段。

## Architecture Issue 与决策

**已解决的局部 Architecture Issue：** AgentScope 把 summary 作为 user-role 消息，且 compression 可能裁切当前输入；Mock shell 原先按最后一个 user-role 取 instruction，因而会混淆历史摘要和本轮原话。处理方式是在本项目 middleware 生命周期边界包装原始输入保真，而不改变 AgentScope summary 格式或关闭 compression。

未发现需要改变 Task / Execution / Resolver 职责的架构问题。Conversation Context = `WRAP`；Authority Context = `KEEP`。本证据不批准更广泛的生产 Context / Memory 重构。

### Proposed Production Impact

| 建议模块 / 文档 | 建议 | 原因与证据 |
|---|---|---|
| `src/cnlc_agent/demo/interaction_middleware.py` | 保留最小 request-local 当前轮输入包装，并确保异常时清理 | AgentScope 2.0.8 的 summary 以 user-role 进入模型消息，压缩可裁切当前输入；C01–C08 回归通过。 |
| `docs/design/04-测井解释智能体技术方案.md` | 记录 Context Compression 的 `WRAP` 职责和 Authority 不变边界 | 原生调用链与集成回归已验证；这次已同步。 |
| `docs/design/05-测井解释智能体测试与验收方案.md` | 增加 A11 / S09 验收口径并纳入退出条件 | 新增独立的当前轮保真及摘要泄漏安全场景；这次已同步。 |

上述生产代码及 Current Design 更新仅是本 Task 的范围内修复与证据同步；没有修改 Resolver、业务 Task / Execution 语义或 01–03 需求目标文档。

## 测试

Task C.2 对应测试及已有 C.1 回归在本工作树执行：

- `tests/integration/test_context_compression_input_boundary.py`：5 passed（覆盖 C02–C08 核心压缩与注入场景；C02–C04 合并在同一多井会话）。
- `tests/integration/test_interaction_robustness.py`：43 passed。
- `tests/integration/test_task_react.py`：30 passed。
- `tests/integration/test_demo_web_http.py`：2 passed。
- `tests/unit`：745 passed，1 条 Starlette `BlockingPortal` deprecation warning。
- 定向组合回归（新增压缩测试、C01、压缩/Task identity 与 operation unit tests）：37 passed。
- Ruff：变更 Python 文件检查通过。
- `git diff --check`：通过。

## 文档影响

- 更新：Design 04 / 05、Current Implementation 08 / 10 / 12、Task 012 总索引、`docs/README.md`、Evidence README；新增本 Evidence 与 Task 执行记录。
- 检查后无需修改：Design 01–03 没有需求、建设目标或预期用户效果变化；`docs/11-status-enum-glossary.md` 没有新增稳定状态或错误码。
- 没有当前实现文档待同步遗留；结果限于 Task 中列出的 Mock / fixture 测试范围。
