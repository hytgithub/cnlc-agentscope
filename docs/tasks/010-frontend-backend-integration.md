# Task 10 前后端真实联调记录

## 测试日期与环境

- 日期：2026-09-25
- 分支：`codex/interactive-agent-2026-09-25`
- PostgreSQL：Compose PostgreSQL 16，宿主端口 `55432`
- Redis：Compose Redis 7，宿主端口 `56379`
- Backend：AgentScope `2.0.8`，`http://127.0.0.1:8000`
- Frontend：AgentScope Web，Vite `8.0.13`，`http://127.0.0.1:5173`
- 持久化：`CNLC_PERSISTENCE=postgres-redis`
- 模型阶段 A：`CNLC_MODEL_PROVIDER=mock`，本地确定性 ReAct 外壳
- 模型阶段 B：DashScope OpenAI compatible `qwen-plus`
- 专业执行：Demo/Mock Tool 与 MockModelGateway；未接 Heavy Prediction API、真实 Report API、LAS/GDSX

API Key 仅从本机未纳入版本控制的环境文件加载，没有输出、复制或写入测试与文档。

## 场景结果

### 1. 首次上传

- 使用 `mock_data/WELL_MOCK_001.json` 和“帮我解释一下这口井”。
- `/chat/` 立即返回 started，聊天先显示 `Execution #1 / QUEUED`，后台继续执行。
- 浏览器中的 Interpretation Panel 自动打开；实际观察到完整任务在运行时的中间状态，另一次全量执行明确观察到 `RUNNING / W02`、W01 已完成、W02 运行中、W03-W10 待执行。
- Execution #1 最终为 `SUCCESS`；四阶段均为 `RUN`，W01-W10 均为 `SUCCESS`。
- Panel 显示 6 个专业 ToolRun：`get_well_data`、`check_curve_quality`、`identify_lithology`、`evaluate_petrophysics`、`calculate_sw`、`merge_intervals`。
- `report_ready=true`，Panel 显示属于 Execution #1 的 Markdown Report。

内置浏览器能完成真实页面、聊天和 Panel 操作，但其原生文件选择器自动化两次超时，且其 Playwright 包装不提供 `setInputFiles`。因此文件内容通过同一个真实 Session 的正式 `/chat/` 上传协议提交，随后在浏览器刷新并验证附件、聊天和 Panel 的完整渲染。此限制属于测试工具，不是产品页面错误。

### 2. POR/PERM 修改与局部重跑

- 同一 Session 输入“把孔隙度、渗透率改成0.16重新解释”。
- AgentScope ReAct 调用 `modify_well_interpretation`，创建 Execution #2 并立即返回 `QUEUED`。
- 最终有效参数为 `POR=0.16`、`PERM=0.16`。
- `DATA_DECODE/PREPROCESS=REUSE`，`INTERPRET/REPORT=RUN`。
- W01-W03 为 `REUSED`，W04-W10 重新运行，当前执行有 4 个专业 ToolRun。

### 3. 运行状态

- UI polling 只请求只读 Task API，约 1 秒一次，并在终态停止。
- 用户输入“现在处理到哪里了？”时才经 ReAct 调用 `get_interpretation_status`。
- qwen-plus 阶段在 Execution #5 运行到 W07 时，返回 `RUNNING`、`current_step=W07` 和已完成 W01-W06，证明回复来自执行事实。

### 4. 上一版报告与 History

- 输入“给我上一版报告”后调用 `get_interpretation_report(selector=PREVIOUS)`。
- Mock 主链返回 Execution #1 报告；报告中的解释目的仍为首次上传指令，可与后续版本区分。
- History 可在 #1/#2/#3/#4 间切换；#1 的参数、步骤、ToolRun 和报告均按该历史执行读取。
- 手动停留在 #1 后触发 Execution #4，轮询期间详情仍保持 #1，直到点击“当前”。

### 5. Full Rerun

- 输入“全部重新跑”后调用 `rerun_well_interpretation`，创建 Execution #3。
- `POR=0.16`、`PERM=0.16` 被保留。
- 四阶段均为 `RUN`，W01-W10 全部重新执行，没有 `REUSED`。

### 6. Backend 重启恢复

- 保留 PostgreSQL 和 Redis，仅停止并重新启动 Python Backend。
- 新进程的 `SessionTaskToolFactory.runners` 初始为空；刷新原 AgentScope Session 后，历史聊天、Panel、Execution 历史和报告全部恢复。
- Read API 通过 `interpretation_session_task_binding` 恢复 ownership。
- 同一旧 Session 输入“把孔隙度改成0.17重新解释”，成功创建 Execution #4；没有 `TASK_NOT_FOUND`。
- Execution #4 最终 `SUCCESS`，`POR=0.17`、`PERM=0.16`，W01-W03 为 `REUSED`。

## qwen-plus ReAct

Mock 主链全部通过后，使用本机密钥切换到 `openai_compatible/qwen-plus`。四条真实模型调用结果如下：

| 输入 | 实际 Tool | 结果 |
| --- | --- | --- |
| 把孔隙度、渗透率改成0.16 | `modify_well_interpretation` | Execution #5 |
| 现在处理到哪里了？ | `get_interpretation_status` | #5 RUNNING，W07 |
| 给我上一版报告 | `get_interpretation_report` | PREVIOUS，Execution #4 |
| 全部重新跑 | `rerun_well_interpretation` | Execution #6 |

Execution #6 最终 `SUCCESS`，有效参数仍为 POR/PERM 0.16，四阶段全部 `RUN`。

## PostgreSQL、Redis 与权限事实

- Alembic current：`0006 (head)`。
- 主联调库最终有 1 Task、1 InputVersion、6 Execution、30 ToolRun、1 Session Binding。
- 关键序列：#1 `INITIAL/W01`，#2 `OVERRIDE_CHANGED/W04`，#3 `FULL_RERUN/W01`，#4 `OVERRIDE_CHANGED/W04`，之后 #5/#6 用于 qwen-plus 验证；序号连续且全部成功。
- Redis `DBSIZE=8`，包含 AgentScope agent/session/messages、后端模型凭证和 CNLC task state。
- 正确 user/agent/session/task 返回 200；错误 user 和错误 session 返回 404。自动测试另覆盖错误 agent 与隔离访问。
- 联调期间只用 SELECT 检查业务表，没有手工 INSERT/UPDATE 业务数据。

## 发现并修复的问题

1. Mock provider 没有公网密钥时，Web 没有可选模型，无法创建可用 Session。增加 AgentScope 可注册的本地 Mock credential/model，固定发布 `qwen-plus` 标识，不保存密钥、不访问公网；自动命名也在本地完成。
2. 同一 Task 创建新 Execution 时 `task_id` 不变，终态 React Query 不会恢复 polling，Panel 会停留在旧执行。现在从最新任务 Tool Result 生成刷新标识，并对稳定 query 主动 refetch 一次。
3. 初版刷新修复把执行标识放入 query key，短暂的空数据会把用户从历史版本切回当前。改为稳定 query key 加显式 refetch，历史选择在后台刷新期间保持不变。
4. 后端模型凭证原先在每个 HTTP 请求中重复 upsert，未进入 lifespan 的只读 API 测试会访问尚未连接的 Redis client。凭证发布移至应用 lifespan，生产启动只写一次。

## Task 10.1：首次解释实时流式展示

旧行为在 `run_well_interpretation` 返回 `QUEUED` 后立即停止消费请求级 Telemetry 队列，输出“解释任务已提交”并结束回复。后台执行虽然继续，但聊天区域看不到 W01-W10、专业 Tool 和报告生成过程。

新行为仍先发送包含 `task_id`、`execution_id` 和 `QUEUED` 的 `ToolResultEnd`，使 Interpretation Panel 能尽早打开；随后保持同一条 AgentScope SSE 回复，消费后台 Worker 继承的 `event_observer`，按真实事件依次展示业务阶段、W01-W10、6 个专业 Tool 及报告生成状态。Execution 进入持久终态后，回复先排空已入队事件，再按本轮 `execution_id` 读取 Markdown 报告，报告输出完成后才发送 `ReplyEnd`。

实时展示复用 `workflow.step.start`、`state.change`、`workflow.result`、`tool.start`、`tool.result`、`tool.error`、`report.start` 和 `report.end`。展示投影位于 `src/cnlc_agent/demo/progress.py`，只读取和去重事件，不控制 Workflow。首次上传的 SSE 展示层在每个真实步骤终态后按 `CNLC_STREAM_STEP_DELAY_SECONDS` 停留，默认 1 秒；后台 Workflow、Execution 和 ToolRun 仍按真实速度执行。没有新增 EventBus、数据库表或轮询接口。

SSE 断开只取消当前观察协程。`ExecutionDispatcher.wait` 的 `shield` 保证后台 Worker 不被取消；自动测试覆盖了取得 `QUEUED` 后关闭流，Execution 仍继续到 `SUCCESS`。页面重新打开仍由 PostgreSQL、SessionTaskBinding 和现有 Read API 恢复 Panel，不做 SSE replay。

真实浏览器使用现有 AgentScope Session 和正式 `/chat/` 上传协议提交 `WELL_MOCK_001.json`。聊天 Thinking 区实际渲染 W01-W10 的开始和完成、6 个 Tool 的开始和成功、报告开始和完成，随后显示本轮 Markdown 报告；右侧 Panel 同时显示 `SUCCESS`、10/10、6 个 ToolRun 和报告，浏览器 Console 无 error。内置浏览器的原生文件选择器仍无法由当前自动化驱动注入文件，因此协议提交后在同一真实页面完成渲染验收。

补充修复：AgentScope Web 的 Thinking 折叠区原先默认关闭，导致事件虽已渲染但用户只能看到“思考中”。现在流式执行期间默认展开，任务完成后保留本轮展开状态；历史消息在页面重新加载后仍可按需展开，避免长报告页面一次铺开全部历史过程。

## Console、Network 与日志

- 浏览器 Console 没有 JavaScript error、Unhandled Promise、React key、Query、CORS 或 404 polling loop。
- Console 有 2 条上游 `mime-types`/Vite 的 `path.extname` browser compatibility warning；不影响任务链路。
- Network 确认 `/chat/`、`/sessions/.../stream`、`/cnlc/interpretation/.../tasks/{task_id}`；运行中 Task Read API 约 1 秒请求一次，终态停止。
- 正常链路和最终两次干净 shutdown 没有 Traceback、pending task、连接泄漏、lease renewal 或 background execution error。

## 实际测试命令与结果

```text
docker compose up -d --wait                              PASS
uv sync --locked --dev                                   PASS
uv run alembic upgrade head                              PASS
uv run alembic current                                   0006 (head)
uv run python -m cnlc_agent.demo.agentscope_app          PASS
pnpm install --frozen-lockfile                           PASS
pnpm dev                                                 PASS
pytest 指定 4 个 integration 文件（真实 PG/Redis 已配置）  8 passed
uv run pytest -q（真实 PG/Redis test URL 已配置）          229 passed, 1 skipped
uv run ruff check .                                      PASS
uv run mypy                                               PASS
ruff format --check（本次修改的 5 个 Python 文件）        PASS
uv run ruff format --check .                              FAIL：基线已有 29 个文件
pnpm --filter frontend build                             PASS
pnpm --filter frontend lint                              PASS（0 error，21 个既有 warning）
```

全仓格式门禁失败可在基线 commit 的 `commands.py` 独立复现；本 Task 修改的 Python 文件均通过格式检查。为避免把联调修复扩大成 29 文件的纯格式化提交，本 Task 没有改写这些无关文件。

## 剩余问题

- 产品链路没有已知阻塞问题。
- 上游前端现有 lint warning 和构建 chunk size warning 未在本 Task 扩大范围处理。
- 全仓 `ruff format --check .` 仍会因基线已有的 29 个未格式化文件失败；本次修改的 5 个 Python 文件通过。
- 内置浏览器文件选择器自动化限制需要通过手工点击或支持文件注入的浏览器驱动复核一次选择动作；正式上传协议、持久化、浏览器渲染及后续交互已完成真实联调。

No Architecture Issue found.
