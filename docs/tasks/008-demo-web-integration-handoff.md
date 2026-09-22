# Task 008：上传井资料到解释报告交接

工作分支：`codex/demo-2026-09-22-web`。基线：`c5db2e4`。

## 当前代码确认的调用链

`frontend/agentscope-web/frontend/src/components/chat/TextInput.tsx::handleSend`
→ `pages/chat/ChatViewport.tsx::fileProcessor`（浏览器附件为 DataBlock/Base64；TXT 为 TextBlock）
→ `hooks/useMessages.ts::send` → `api/chat.ts::chatApi.trigger`
→ `POST /chat/`（AgentScope 2.0.8 `_router/_chat.py::chat`）
→ 官方 ChatRunRegistry → `_service/_chat.py::ChatService.run` 创建 Demo Agent，调用 `reply_stream`
→ `demo/demo_agent.py::LoggingInterpretationDemoAgent`
→ `demo/upload_reply.py::UploadInterpretationReply.on_reply`（官方 Middleware 扩展点）
→ `demo/uploads.py::parse_upload` 校验本轮 DataBlock/Base64 或 TXT TextBlock
→ `Toolkit.call_tool` → `demo/tools.py::RunWellInterpretationTool.call`
→ `run_uploaded_well` 将本次资料写入独立临时目录，配置已有 MockWellRepository
→ `InterpretationToolRunner.run` → `application_runtime` → `InterpretationTaskService.run`
→ `MainAgent.run` → `InterpretationWorkflow.run` → W01–W10
→ `ReportAssembler.to_markdown` → Tool Result / Assistant 文本事件
→ 官方 `GET /sessions/{session_id}/stream?agent_id=...`
→ `hooks/useMessages.ts` → `components/chat/ASMessageBubble.tsx` / `components/markdown`。

进度旁路：`LoggingTelemetry.event` → 请求级 `event_observer`（ContextVar）
→ asyncio Queue → `TextBlockDeltaEvent` → 同一个官方 Session SSE。
没有轮询伪进度，没有第二套 Chat/Upload REST API，也没有修改 W01–W10。

## 开始时的缺口

- 输入框根据 qwen-plus 模型能力过滤附件，不接受井资料 JSON。
- Tool 只有 well_id，没有将聊天附件导入当前 FixtureRepository 的桥接。
- Workflow Trace 只写日志，没有送到 SSE；Tool 结束后才有状态。
- 最终报告依赖外层模型再次转述，不能保证原文完整。

## 已完成内容

- 自然语言 + 单个附件直接启动解释。well_id 从文件读取，缺省时生成 `UPLOAD_<uuid>`；
  task_id 由现有 TaskRequest 生成；instruction 来自本轮自然语言。用户不填业务参数表单。
- 上传井资料实际进入现有 Repository 和 W01–W10，测试使用自定义井名和流体结果确认
  没有回退到仓库 WELL_MOCK_001。临时目录在完成/异常/中断后清理，不覆盖仓库数据。
- 同井号的并发上传隔离，Trace 也按执行上下文隔离。
- 实时展示每步开始和最终状态、报告生成。测试让 W06 模型等待，确认等待期间已收到
  W06 开始事件；停止操作取消实际业务任务并关闭 Tool/Reply 事件。
- Tool Result 保留结构化结果，最终 Assistant 文本直接使用 `report_markdown` 原文。
  Markdown 保留 Demo / Mock / Demo Skip 标记。
- 格式错误、缺少附件、超限附件、服务异常有安全的返回；不输出原始模型异常。
- 官方 HTTP/Session/SSE 集成测试已通过：真实调用官方路由、ChatService、Toolkit、
  业务服务、Workflow、ReportAssembler 和官方 RedisStorage；仅 Redis I/O 使用 fakeredis、
  外层模型构造使用测试替身、业务模型使用 Mock。测试还确认会话消息中保存了报告。

## 已修改文件

| 文件 | 用途 |
| --- | --- |
| `src/cnlc_agent/demo/uploads.py` | 新增：官方附件解码、大小限制、现有 JSON Schema 校验、井号生成 |
| `src/cnlc_agent/demo/upload_reply.py` | 新增：官方 Middleware、Tool 调用、实时事件、原文报告和取消清理 |
| `src/cnlc_agent/demo/demo_agent.py` | 注册上传 Middleware，保持单业务 Tool |
| `src/cnlc_agent/demo/tools.py` | 本次上传绑定、临时数据目录、复用 service、异常隔离 |
| `src/cnlc_agent/infrastructure/telemetry.py` | 增加可选请求级事件观察者，原有日志行为保留 |
| `frontend/agentscope-web/frontend/src/pages/chat/ChatViewport.tsx` | 接受 JSON/TXT，与模型多模态能力解耦；读取文件内容而非本地路径；限制 5 MiB |
| `frontend/agentscope-web/frontend/src/components/chat/ChatContent.tsx` | 输入提示改为自然语言 + 井资料附件 |
| `tests/integration/test_demo_web_upload.py` | 新增：上传完整流程、隔离、失败、真实进度时序和中断测试 |
| `tests/integration/test_demo_web_http.py` | 新增：官方 HTTP Chat/SSE/消息持久化集成测试 |
| `pyproject.toml`、`uv.lock` | 仅增加开发测试依赖 fakeredis 及其依赖，运行时 AgentScope 仍为 2.0.8 |
| `README.md` | 修正前端目录、配置、附件交互、测试和格式边界 |
| 本文 | 联调事实、启动步骤和后续交接 |

## 格式边界 / Integration Dependency

当前只接收已有 `MockFixture` JSON（可参考 `mock_data/*.json`），自动读取或生成井标识。
真实原始 LAS/GDSX/CSV 的解析和专业算法尚未提供，不能由接入层补造。
Pending final well-data schema.

Task 005/006 已在当前基线中，无待合并依赖。真实 qwen-plus、用户本地 Redis/PG、浏览器
点击上传的联合验收尚未在本次环境执行，不能把离线 HTTP/SSE 测试当作这些已验收。

## Demo 启动

1. 启动本地 Redis。仓库根目录配置 `.env`（不要提交密钥）：

```dotenv
REDIS_URL=redis://localhost:6379/0
CNLC_MODE=demo
CNLC_PERSISTENCE=memory
CNLC_MODEL_PROVIDER=openai_compatible
MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MODEL_NAME=qwen-plus
MODEL_API_KEY=在本地填写
```

离线流程演示可改 `CNLC_MODEL_PROVIDER=mock`。PG 持久化沿用已有 `postgres-redis` 配置。

2. 后端，仓库根目录：

```bash
uv sync --locked --dev
uv run python -m cnlc_agent.demo.agentscope_app
```

3. 前端，新终端（Node/pnpm 需满足仓库 Vite 版本要求；本次用 Node 24.19.0 构建）：

```bash
cd frontend/agentscope-web
pnpm install --frozen-lockfile
pnpm dev
```

4. 打开 `http://localhost:5173`，Server URL 为 `http://localhost:8000`。沿用官方 UI
   创建 Agent 和 Session；无需在 UI 创建 Credential。AgentScope 会只读共享后端根据 `.env`
   托管的模型凭证，并自动选择 qwen-plus；W06/W07 同样读取 `.env` 模型配置。
   两处均需有效配置，Web 业务上传不新增任何井号/任务号表单。
5. 附件选择 `mock_data/WELL_MOCK_001.json` 或 `WELL_DI73_56H_LAYER64.json` /
   `WELL_DI73_56H_LAYER65.json`，输入“帮我解释一下这口井”后发送。
6. 检查 W01–W10 状态、Tool Result、报告及 Demo Skip。请避免把历史/Mock 结果描述为
   上传原始曲线后独立算出的真实结论。

## 验证结果

- `uv run pytest -q`：83 passed，4 skipped（真实模型 1 项、真实 PG/Redis 3 项未配置）。
- `uv run ruff check .`：通过。
- `uv run ruff format --check .`：通过，78 files。
- `uv run mypy`：通过，42 source files。
- `uv build`：wheel 和 sdist 通过。
- `pnpm --dir frontend build`（在 `frontend/agentscope-web`）：TypeScript + Vite 通过。
- 上游构建已有 `mime-types` 的 Node path 浏览器兼容提示、大 chunk 提示；未做前端重构。
- pytest 有上游 Starlette/AnyIO 弃用提示，不影响结果。

## 未完成内容 / Sol 交接

1. 用本地真实配置完成一次浏览器验收，确认 qwen-plus W06/W07、停止按钮、最终报告；
   本次 HTTP/SSE 验证已覆盖链路，但未代替浏览器现场验收。
2. 可在现有 Tool Renderer 扩展点增加更易读的步骤摘要，当前进度直接显示在 Assistant
   Markdown，Tool Result 仍是官方默认可展开的 JSON，未做卡片/颜色等装修。
3. 补上传错误 toast 和简单文案。当前原生 TextInput 在 fileProcessor 抛错后会移除附件，
   因此浏览器选择 >5 MiB 文件时不会显示详细原因；后端仍会明确拒绝超限请求。
4. 如需更多输入格式，先确认正式井资料 Schema 和专业 Tool 输入能力，不要生成虚构的
   outputs 来强行跑完 Workflow。这不是单纯改上传文件扩展名能解决的问题。

## 当前已知问题与范围

- 这是单轮、单井 Demo：本轮需同时发送文本和文件；不复用上一轮附件，不支持跨文件
  合并、历史任务追问、通用闲聊或复杂排队会话。无附件会提示上传。
- Middleware 将带有效附件的本轮消息视为解释请求，直接调唯一业务 Tool，不做额外
  LLM 意图识别和报告改写。W06/W07 仍走既有 ModelGateway；所有实际模型统一 qwen-plus。
- Middleware 截获这类 Demo 回复，未调用通用 ReAct 的后续 Middleware 链；不支持其
  Inbox/Team/HITL/TTS 等附加交互。官方 Chat 路由、会话锁、SSE、消息保存仍被复用并已测。
- 上传 JSON 本身必须携带专业 Mock/历史结果；原始曲线文件还不能独立得到完整解释。
- HTTP 测试通过 fakeredis 验证 RedisStorage 调用契约，不等于测试真实 Redis 服务。
- 前端和报告使用既有渲染器；未开发新前端、未修改 Workflow、专业 Tool、ModelGateway、
  ReportAssembler 或数据库/Redis 架构。

## Architecture Issue

No Architecture Issue found.
