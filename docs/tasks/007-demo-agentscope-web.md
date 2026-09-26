# Demo Task 007：AgentScope Web Integration（最终执行版）

目标：基于已经复制到项目中的 AgentScope 官方 Web UI，把现有测井解释后端接到前端，并在 Tool Result 中可视化 W01-W10 解释过程与最终报告。

## 1. 当前状态

工作分支：

`codex/demo-2026-09-22-web`

集成分支：

`demo/2026-09-22`

`main` 保持冻结，不允许修改。

当前 Web 分支已经具备：

- AgentScope 2.x 后端适配：
  - `src/cnlc_agent/demo/agentscope_app.py`
  - `src/cnlc_agent/demo/demo_agent.py`
  - `src/cnlc_agent/demo/tools.py`
- AgentScope 官方 `examples/web_ui` 已复制到：
  - `frontend/agentscope-web`
- 前端已可本地启动。
- AgentScope 原生 Chat / Session / SSE 代码已存在。
- `run_well_interpretation` Tool 已存在，并复用现有 `InterpretationTaskService`。
- Demo Workflow / Report / qwen-plus ModelGateway 已存在。

当前不需要重新设计 AgentScope Service，也不需要新增第二套 REST Chat API。

## 2. 开始前先同步 Demo 基线

当前 Web 分支可能落后 `demo/2026-09-22`。

开始实施前执行：

```bash
git fetch origin
git checkout codex/demo-2026-09-22-web
git merge origin/demo/2026-09-22
```

如果出现冲突，只解决本任务相关文件；不得修改 main。

同步后确认最新 Demo Mock 数据也已存在。

## 3. 最终架构

```text
frontend/agentscope-web
        ↓
AgentScope Chat API / SSE
        ↓
AgentScope Agent Service
        ↓
LoggingInterpretationDemoAgent
        ↓
run_well_interpretation(well_id)
        ↓
InterpretationTaskService
        ↓
MainAgent
        ↓
W01 → W10
        ↓
InterpretationState
        ↓
Demo Presenter
        ↓
Tool Result DTO
        ↓
RunWellInterpretationRenderer
        ↓
W01-W10 解释过程 + Markdown Report
```

## 4. 本任务核心目标

用户在前端输入：

```text
对 WELL_MOCK_001 进行常规测井解释
```

或其他 Demo 井号。

前端最终应展示：

1. 用户消息；
2. Agent 回复；
3. `run_well_interpretation` Tool Call；
4. W01-W10 解释过程；
5. 每一步状态；
6. 每一步输入摘要；
7. 每一步输出摘要；
8. Evidence；
9. Warnings；
10. W06/W07 标记 `qwen-plus`；
11. W09 标记 `Demo Skip`；
12. 最终 Markdown 报告。

## 5. 不新增独立前端业务 API

当前官方前端已经使用：

- `POST /chat/`
- `/sessions/`
- `GET /sessions/{session_id}/messages`
- `GET /sessions/{session_id}/stream`

并通过 SSE 接收 Agent Event。

继续使用 AgentScope 原生协议。

不要新增：

```text
POST /demo/interpret
```

之类的第二套平行聊天接口。

## 6. 后端改造：Demo Presenter

当前 `DemoToolResult` 只有：

- status
- task_id
- well_id
- completed_steps
- step_statuses
- summary
- report_markdown

这不足以展示具体解释过程。

新增：

```text
src/cnlc_agent/demo/presentation.py
```

职责：

```text
InterpretationState
        ↓
前端展示 DTO
```

不要修改 Workflow 来迎合前端。

### Step DTO

建议每一步返回：

```json
{
  "id": "W06",
  "name": "流体识别",
  "status": "SUCCESS",
  "source": "qwen-plus",
  "input_summary": {},
  "output_summary": {},
  "evidence": [],
  "warnings": []
}
```

完整 Tool Result 建议：

```json
{
  "task_id": "demo-001",
  "well_id": "WELL_MOCK_001",
  "status": "SUCCESS",
  "steps": [],
  "summary": {},
  "report_markdown": "# 单井测井解释演示报告..."
}
```

不要把完整巨大 `InterpretationState` 原样返回给前端。

不要返回：

- API Key
- Secret
- 原始 Exception
- 不必要的内部 Trace

## 7. W01-W10 展示映射

固定名称：

```text
W01 原始资料加载
W02 数据完整性检查
W03 数据预处理与质量控制
W04 岩性识别
W05 储层识别与物性评价
W06 流体识别
W07 油气水层分类
W08 层段划分与有效厚度
W09 综合验证
W10 最终一致性检查与报告
```

source 至少区分：

```text
tool
mock
qwen-plus
demo-skip
workflow
```

Demo 中：

- W03：mock
- W04：mock
- W05：mock
- W06：qwen-plus
- W07：qwen-plus
- W08：mock
- W09：demo-skip
- W01/W02/W10：按实际来源标记 tool/workflow

## 8. 后端文件改动

优先修改：

```text
src/cnlc_agent/demo/presentation.py   ← 新增
src/cnlc_agent/demo/tools.py          ← 返回完整 steps DTO
```

原则上不要修改：

```text
src/cnlc_agent/workflows/
src/cnlc_agent/infrastructure/model_gateway.py
src/cnlc_agent/reports/assembler.py
```

除非发现真实集成阻塞，并明确记录 Integration Dependency。

## 9. 前端改造：专用 Tool Renderer

AgentScope 官方 Web UI 已有扩展点：

```text
frontend/agentscope-web/frontend/src/components/chat/tool-renderers/
```

当前 `index.tsx` 已通过 tool name 注册不同 renderer。

新增：

```text
RunWellInterpretationRenderer.tsx
```

并在：

```text
tool-renderers/index.tsx
```

注册：

```text
run_well_interpretation
→ RunWellInterpretationRenderer
```

其他 Tool 仍然使用 AgentScope 官方 Renderer，不修改。

## 10. RunWellInterpretationRenderer

Header 建议展示：

```text
执行单井测井解释 · WELL_MOCK_001 · SUCCESS
```

Body 展示 W01-W10：

```text
✅ W01 原始资料加载
✅ W02 数据完整性检查
✅ W03 数据预处理与质量控制    Mock
✅ W04 岩性识别                Mock
✅ W05 储层识别与物性评价      Mock
🤖 W06 流体识别                qwen-plus
🤖 W07 油气水层分类            qwen-plus
✅ W08 层段划分与有效厚度      Mock
⏭ W09 综合验证                Demo Skip
✅ W10 最终一致性检查与报告
```

点击步骤后显示：

```text
步骤名称

输入
input_summary

处理来源
source

输出
output_summary

证据
evidence

警告
warnings
```

实现方式优先保持简单，可使用折叠区域或当前项目已有 Card / Collapsible 组件。

不要增加复杂全局状态。

## 11. Markdown 报告

当前官方前端已经有：

```text
frontend/src/components/markdown/index.tsx
```

直接复用。

不要重新引入第二套 Markdown 库。

在解释过程下方展示：

`report_markdown`

第一版允许上下排列：

```text
解释过程
────────
W01-W10

最终报告
────────
Markdown
```

不要求 Tabs。

## 12. 前后端连接

前端当前 API Client 从：

```text
localStorage.server_url
```

读取后端地址。

Demo 后端：

```text
http://localhost:8000
```

不要修改 API Client 来硬编码地址。

通过现有 Setup 页面或当前配置方式设置：

```text
server_url = http://localhost:8000
```

后端当前已配置 Demo CORS，可继续使用。

## 13. AgentScope Service

现有：

```bash
uv run python -m cnlc_agent.demo.agentscope_app
```

继续沿用。

不要重新实现 Agent Service。

AgentScope Service 当前使用 RedisStorage，因此本地运行前确保 Redis 可用。

## 14. 模型

所有需要 LLM 的 Demo 能力统一：

```text
qwen-plus
```

包括：

- LoggingInterpretationDemoAgent
- W06
- W07

不增加其他模型。

凭据只从本地环境读取。

## 15. 演示场景

至少支持：

```text
对 WELL_MOCK_001 进行常规测井解释
```

如果 Demo 基线已有其他井 fixture，也允许使用。

最终页面需要出现：

```text
User
  ↓
Agent
  ↓
run_well_interpretation
  ↓
W01-W10 Interpretation Process
  ↓
Final Markdown Report
```

## 16. 测试

### 后端

补充或修改：

```text
tests/integration/test_demo_agentscope_web.py
```

至少验证：

- Tool 调用现有 InterpretationTaskService；
- steps 包含 W01-W10；
- W06/W07 source = qwen-plus（真实模型测试可单独 opt-in；离线测试可验证 DTO 映射）；
- W09 source = demo-skip；
- report_markdown 存在；
- 无 Secret 泄漏；
- Agent Service 可构造。

### 前端

至少验证：

- `run_well_interpretation` renderer 能解析成功 payload；
- steps 可以正常渲染；
- 缺少可选字段时不崩溃；
- Markdown 报告可展示；
- TypeScript build 通过。

不要求为了 Demo 新引入大型测试框架。

## 17. 验收命令

后端：

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
```

前端：

```bash
cd frontend/agentscope-web
pnpm build
pnpm lint
```

如果当前 package.json 有其他必要校验，也执行。

## 18. 人工联调

启动 Redis。

启动后端：

```bash
uv run python -m cnlc_agent.demo.agentscope_app
```

启动前端：

```bash
cd frontend/agentscope-web
pnpm dev
```

前端 server URL 设置为：

```text
http://localhost:8000
```

输入：

```text
对 WELL_MOCK_001 进行常规测井解释
```

确认：

1. Agent 回复；
2. Tool Call 出现；
3. W01-W10 卡片出现；
4. W06/W07 显示 qwen-plus；
5. W09 显示 Demo Skip；
6. 最终 Markdown 报告出现。

## 19. 明确不做

本任务不做：

- 自研聊天协议；
- 新增独立 REST Demo API；
- 登录注册；
- 权限体系；
- 文件上传；
- 多井管理；
- 图表系统；
- 曲线绘图；
- PDF；
- RAG；
- Rollback；
- 新专业算法；
- 重写 W01-W10；
- 修改 main。

## 20. Git 要求

完成后：

```bash
git status
git diff --stat
```

确认没有：

- API Key
- .env
- node_modules
- dist
- 缓存
- Secret

提交并 push 到：

```text
origin/codex/demo-2026-09-22-web
```

不要合并到 main。

## 21. 最终报告

完成后输出：

1. 实现内容
2. 新增/修改文件
3. Tool Result DTO
4. 前端 Renderer
5. Agent Service 启动命令
6. Web UI 启动命令
7. 前后端连接配置
8. W01-W10 实际展示效果
9. pytest
10. ruff
11. mypy
12. uv build
13. pnpm build
14. pnpm lint
15. commit SHA
16. Integration Dependency
17. Architecture Issue

如果没有架构问题：

```text
No Architecture Issue found.
```
