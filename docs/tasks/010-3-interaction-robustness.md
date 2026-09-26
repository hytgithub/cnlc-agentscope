# Task 10.3 implementation summary

## 基线与范围

- 日期：2026-09-26。
- 分支：`codex/demo-2026-10-31`。
- 已执行 fetch / checkout / pull，开始 HEAD：`62f9867dad2fafa3537048a21f87b9f261c042ee`。
- 提交名称：`feat: harden interaction state handling`；最终 SHA 见本提交及任务完成回复。
- 本次只处理 Interaction Layer、必要的安全状态读模型与回归测试。未修改 W01–W10、
  公司批次依赖、external_call_id、DERIVED ToolRun、公司客户端或结果适配器。
- 未修改前端、main，未创建 PR。

## 实现与核心设计

1. `InteractionSnapshot` 从 SessionTaskResolver、授权 Binding、Task、当前 Execution 与
   Session middle_context 派生，不存业务 Task，不新增数据库表或 migration。
2. InteractionPhase 为 NO_TASK / READY / ACTIVE / NEED_CLARIFICATION，ExecutionStatus 不复制。
3. InteractionPolicy 集中输出 ALLOW / READ_ONLY / CLARIFY / REJECT。无任务、活跃执行写入、
   当前报告未就绪、歧义修改、未支持能力／参数和冲突都有明确裁决。
4. 新增纯交互工具 request_interpretation_clarification，只记录或返回交互状态，不创建执行。
   模型仍是单次 ReAct 语义入口，没有第二次意图模型调用或生产正则 IntentClassifier。
5. PendingClarification 一次完整写入 AgentScope middle_context，保存值、缺项、固定授权任务、
   引用、owner token、轮次与 TTL。只允许紧邻下一轮；默认 TTL 600 秒可配置。
   参数名通过原 modify 工具的 parameter_name 补齐；无值不能从旧聊天猜测。
   成功补齐、完整新操作、无关下一轮、拒绝、过期、不完整数据或新 runner 后清除。
6. Session State JSON 重载可保持完整 pending 和 active；压缩不用摘要恢复数值。
   Backend 新 runner 无法证明旧 pending 生命周期，安全丢弃，即使 AgentScope 恢复它。
   业务绑定仍由真实 PostgreSQL 恢复；active 不可恢复时 CURRENT 选最近 Binding。
7. 引用解析只选择授权目标，成功业务访问才切 active。上一版与上一口井严格分离。
8. 运行中 MODIFY / FULL_RERUN 返回 TASK_EXECUTION_ACTIVE，提示 Execution 序号和当前步骤，
   无新版本；Application 原子并发保护继续保留。
9. STATUS 安全投影 failed_step、missing_data/affected_step、warning_count、review_required 和
   稳定错误代码。FAILED 不输出 exception 原文；BLOCKED 告知缺什么、哪里停止；
   REVIEW_REQUIRED 明确人工复核；WARNING 明确已完成且有告警。
10. CURRENT 绝不借用旧报告。FAILED/BLOCKED 本版若已有诊断报告可以读取，无报告则明确未就绪。
11. 领域内未支持参数、Sw/岩性/层段局部重算、细层段证据与专业问答返回能力边界；
    天气、Java、笑话属于领域外，不调用测井 Task Tool。
12. 同句冲突用交互工具澄清。即使模型一次产生两个写 Tool，也由 on_acting 在任何写入前拒绝整批。
13. 澄清、稳定错误、STATUS 与 GET_REPORT 在工具事实返回后直接渲染，避免模型反复重试或改写。
    创建执行的 START/MODIFY/FULL_RERUN 仍使用原 ExecutionReplyStreamer，SSE 断流不取消后台执行。
14. 模型 Schema 只发布 task_reference，避免旧 task_id 与新引用同时填写；Python adapter 保留旧调用。

正式设计、工具 Contract 与完整 Scenario Matrix 见 [../10-interaction-state-machine.md](../10-interaction-state-machine.md)。

## 文件

新增：

- `src/cnlc_agent/demo/interaction_state.py`
- `src/cnlc_agent/demo/interaction_middleware.py`
- `tests/unit/test_interaction_state.py`
- `tests/integration/test_interaction_robustness.py`
- `docs/10-interaction-state-machine.md`
- `docs/tasks/010-3-interaction-robustness.md`

修改：

- `src/cnlc_agent/demo/task_tools.py`：快照、短期澄清、集中裁决和工具 Schema。
- `src/cnlc_agent/demo/demo_agent.py`：固定语义规则与 Mock ReAct 场景，注册交互 middleware。
- `src/cnlc_agent/application/commands.py`：安全诊断读模型。
- `src/cnlc_agent/config/settings.py`、`.env.example`：澄清 TTL。
- `tests/unit/test_task_tools.py`：保留应用语义测试，扩展不支持参数错误。
- `tests/integration/test_task_react.py`：保留多井、压缩和流式测试，适配受控直接回复。
- `tests/integration/test_session_task_binding.py`：真实 PG/Redis 重建 runner 时清除 pending。
- `docs/05-interactive-agent-detailed-design.md`
- `docs/08-intent-and-interaction-design.md`
- `docs/README.md`
- `README.md`

工作区原有未跟踪 IDE 文件、demo_output 和 GDSX 工具清单不属于本提交。

## 测试环境与结果

使用 uv / Python 3.11 / AgentScope 2.0.8；测试专业数据和算法为 Mock。
真实持久化测试使用独立 Docker Compose PostgreSQL 16 / Redis 7，端口 55433 / 56380，
Redis DB 15。原 55432 已被其他容器占用，未停止或改变该服务。
真实服务测试按现有 fixture 执行 migration 与测试数据清理；没有手工改业务状态或插入业务记录。
测试结束后停止本轮创建的两个测试容器，保留 volume。

本地未跟踪 `.env` 的 CNLC_LOG_LEVEL 值含多余字符，初次测试收集失败。
所有后续测试使用命令级 `CNLC_LOG_LEVEL=INFO` 覆盖，未改写用户 `.env`。
数据库测试 URL 与模型密钥仅在进程内从本地配置读取，未写入仓库或测试输出。

| 命令 / 验证 | 结果 |
| --- | --- |
| uv run pytest -q tests/unit/test_interaction_state.py | 47 passed |
| uv run pytest -q tests/unit/test_task_context.py | 2 passed |
| uv run pytest -q tests/unit/test_task_tools.py | 8 passed |
| uv run pytest -q tests/integration/test_task_react.py | 25 passed |
| uv run pytest -q tests/integration/test_session_task_binding.py | 3 passed，真实 PG/Redis |
| uv run pytest -q tests/integration/test_demo_web_upload.py | 16 passed |
| 上述文件与 test_interaction_robustness.py 合并 | 137 passed |
| uv run pytest -q（真实服务 test URL 配置） | 368 passed，2 skipped，1 warning |
| uv run ruff check . | FAIL，222 个既有错误，诊断输出与开始 HEAD 完全相同 |
| uv run mypy | FAIL，243 个既有错误 / 16 文件，较开始 HEAD 无新增错误 |
| uv run ruff format --check . | FAIL，46 文件；开始 HEAD 有 49 文件 |
| 本次修改/新增 Python 文件的 ruff check（11 文件） | PASS |
| 本次全部生产 Python 文件与三份新增/改动测试的 format check（9 文件） | PASS |
| git diff --check | PASS |

全量 skipped：现有 opt-in ModelGateway 网络测试、没有配置真实 GDSX 样本的测试。
本轮 qwen-plus 外层 ReAct 已另行实测，不能把这两个 skipped 解读为真实模型交互没有测试。
warning 是现有 Starlette/anyio 弃用提示。

质量门禁基线由 `git archive 62f9867...` 到临时目录，使用同一 venv 的 ruff/mypy 复核。
全仓 lint/type 错误集中在既有 pygdsx/wplm 导入与类型/风格，未扩大本 Task 去修复。
没有把全仓门禁失败写成 PASS，也没有新增 ignore 配置隐藏这些错误。

## 场景回归

- 无任务 STATUS/REPORT/MODIFY/RERUN/歧义修改：明确错误，无 Task/Execution。
- “改成0.16”→“孔隙度”：仅一个 MODIFY Execution；完整报告仍流式返回。
- RUNNING W06 写入拒绝；重复 STATUS 每次读取同 Execution 当前步骤；CURRENT 未就绪，PREVIOUS 严格选版。
- 上一版不存在、上一口井不存在、未知井、不支持参数/局部重算、混合操作：无新增执行。
- 参数相同：NO_EFFECTIVE_CHANGE，提示当前值，无空执行。
- Tool 失败：安全错误码和失败步骤；诊断报告属于本版；无报告不降级。
- FAILED 显式全量重跑走原 Application；BLOCKED 缺项、WARNING 报告/修改、REVIEW_REQUIRED 状态均覆盖。
- 多井 active、上一版≠上一口井、指定井、压缩后任务选择沿用现有回归。
- 指定井澄清固定目标；Session State JSON 重载保持对象，不串井。
- 真实 PG/Redis Binding 重建恢复与权限隔离；不可恢复 pending 安全清除。
- fixture/company_mock START 与 STATUS smoke，以及两种上传/流式路径通过。
- 明确领域外请求无 Tool；模型违反规则输出冲突写 Tool 时整批保护通过。
- Browser refresh 的 Session 序列化与读模型恢复由自动测试覆盖；本轮没有另开浏览器手工刷新验收。

## qwen-plus 实测

本地存在有效模型凭据，使用真实 DashScopeCredential / qwen-plus ReAct。
仅外层交互访问真实模型；Workflow 专业来源仍为 fixture，持久化用独立 memory runner。
没有把专业 Mock 的成功称为真实专业算法或公司 Heavy API 成功。

最初发现模型混填 task_id/task_reference、重试无效 Schema，以及只输出工具计划的问题。
随后收敛 Schema、固定工具结果回复、缩短系统规则并明确实际工具调用义务。最终同一会话实测：

| 输入 | 实际结果 | Execution 数 |
| --- | --- | --- |
| 改成0.16 | request_interpretation_clarification，CLARIFICATION_REQUIRED，pending 有效 | 1 |
| 孔隙度 | modify_well_interpretation(parameter_name=por)，有效 POR=0.16，pending 清除，流式报告 | 2 |
| 现在执行到哪里了 | get_interpretation_status，Execution #2 SUCCESS | 2 |
| 重新计算含水饱和度 | 纯交互 Tool，UNSUPPORTED_OPERATION | 2 |
| 上一口井的报告 | get_interpretation_report(PREVIOUS_TASK + LATEST_SUCCESSFUL)，PREVIOUS_TASK_NOT_FOUND | 2 |

这些是五个具体场景的实测，不代表对任意自然语言措辞的模型准确率保证。

## 未完成内容与下一阶段依赖

- 同 Task 更新 InputVersion、曲线补齐后从中间恢复仍未实现；同井新上传仍创建新 Task。
- 不实现独立 IntentClassifier、Skill、CapabilityGraph、Sw-only、Compare、Query/Evidence 系统、
  Pause/Resume/Cancel、执行队列或 WPLM/GDSX 主链接入。
- Browser 手工刷新未重新验收；前端未修改，无需本 Task 前端 build/lint。
- 全仓既有 lint/type/format 门禁问题需要独立清理任务。
- 后续细粒度查询/重算与输入替换需要各自明确 Contract 和 Application policy；
  真实专业链路需要正式公司接口、脱敏井数据及业务输出映射。

No Architecture Issue found.
