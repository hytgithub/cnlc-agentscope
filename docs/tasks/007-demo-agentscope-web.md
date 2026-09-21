# Demo Task 007：AgentScope Web Integration

目标：复用 AgentScope 2.0 官方 Web UI，快速完成“前端 → AgentScope Agent Service → 测井解释业务 → 报告”的演示闭环。

## 基线

- 集成分支：`demo/2026-09-22`
- 本任务分支：`codex/demo-2026-09-22-web`
- `main` 保持冻结，不允许修改。
- Demo 基线已经包含 Task 01～04，包括 OpenAI-Compatible ModelGateway 和 qwen-plus。

## 官方接入方式

AgentScope 2.0 当前官方推荐：

- 后端：Agent Service（FastAPI）
- 前端：主仓库 `examples/web_ui`
- 官方前端直接复用，不自行开发新的 Web 页面。
- 旧的独立 AgentScope Studio 不作为本 Demo 方案。

## Demo 架构

```text
AgentScope examples/web_ui
        ↓
AgentScope Agent Service
        ↓
LoggingInterpretationDemoAgent
        ↓
run_well_interpretation(well_id)
        ↓
现有 InterpretationTaskService
        ↓
MainAgent
        ↓
W01 → W10
        ↓
InterpretationState
        ↓
JSON + Markdown Report
```

## 实现范围

### 1. AgentScope 依赖

- 在当前项目加入 AgentScope 2.x 所需依赖。
- 不复制官方 Web UI 源码进本仓库。
- 前端直接使用 AgentScope 官方仓库 `examples/web_ui`。

### 2. Demo Agent

建议新增：

```text
src/cnlc_agent/demo/
├── __init__.py
├── agentscope_app.py
├── demo_agent.py
└── tools.py
```

命名可以按现有项目规范调整。

Demo Agent 只负责对话入口和 Tool 调用，不重新实现测井业务。

模型统一使用：

```text
qwen-plus
```

### 3. 唯一业务 Tool

第一版只暴露一个高层 Tool：

```text
run_well_interpretation(well_id: str)
```

内部调用现有业务能力，不重新复制 Workflow：

```text
well_id
  ↓
TaskRequest
  ↓
InterpretationTaskService.run(...)
  ↓
MainAgent
  ↓
W01-W10
```

返回给 AgentScope 的结果至少包含：

- status
- well_id
- completed_steps
- step statuses
- summary
- report_markdown

可以包含必要的结构化解释结果，但避免返回超大原始状态。

### 4. 前端交互目标

用户在官方 Web UI 输入类似：

```text
对 WELL_MOCK_001 进行常规测井解释
```

期望看到：

1. Demo Agent 接收消息；
2. 调用 `run_well_interpretation`；
3. Tool Call 可在官方 Web UI 中显示；
4. Tool Result 返回 W01-W10 执行状态；
5. Agent 最终返回解释摘要和 Markdown 报告。

不要求本任务修改官方前端来定制 W01-W10 进度条。

### 5. Agent Service

参考 AgentScope 当前官方 `examples/agent_service/main.py` 的方式：

- 使用 AgentScope Agent Service；
- 提供可运行的 FastAPI/uvicorn 服务；
- 默认演示端口优先使用 `8000`；
- 配置必要 CORS，使本地官方 Web UI 可连接；
- 不新增独立自定义 Web API 协议来绕过 AgentScope。

建议提供类似：

```bash
uv run python -m cnlc_agent.demo.agentscope_app
```

作为后端启动命令。

### 6. 官方前端运行说明

README 增加 Demo Web 说明，使用官方仓库：

```bash
git clone -b main https://github.com/agentscope-ai/agentscope.git
cd agentscope/examples/web_ui
pnpm install
pnpm dev
```

并说明如何配置前端连接本项目 Agent Service。

不要将官方前端源码复制到本仓库。

## 模型要求

所有 Demo 中需要 LLM 的位置统一使用 qwen-plus：

- DemoShell/Demo Agent
- InterpretationAgent W06
- InterpretationAgent W07

不得额外引入其他模型。

凭据只允许通过本地环境变量读取，不得写入仓库。

## 不在本任务实现

- 不修改 W01-W10 业务流程；
- 不修改 ModelGateway 核心实现；
- 不修改专业 Mock/Real Tool；
- 不修改 ReportAssembler 业务内容；
- 不开发自定义前端页面；
- 不实现生产权限体系；
- 不实现正式认证；
- 不实现生产部署；
- 不实现复杂多 Agent UI；
- 不合并到 main。

## 测试

至少覆盖：

1. `run_well_interpretation` 可以调用现有 service；
2. Tool 返回稳定结构；
3. Demo Agent 可以注册/调用该 Tool；
4. Agent Service 可以启动；
5. Mock 模式下测试不依赖公网；
6. 无鉴权凭据泄漏；
7. 如果 Task 005/006 尚未合并，允许记录 Integration Dependency，不擅自修改它们的核心文件。

## 验收

最终至少能完成：

```text
官方 AgentScope Web UI
  ↓
发送 WELL_MOCK_001 解释请求
  ↓
Agent Tool Call 可见
  ↓
run_well_interpretation
  ↓
现有测井解释业务
  ↓
返回 W01-W10 状态
  ↓
返回最终 Markdown 报告
```

并通过项目现有的 pytest / ruff / mypy / build（若依赖安装环境允许）。

## 文件边界

优先修改：

- `src/cnlc_agent/demo/`
- AgentScope 相关依赖
- Demo Web 集成测试
- README 的 Demo Web 运行说明

原则上不要修改：

- `src/cnlc_agent/workflows/`
- `src/cnlc_agent/infrastructure/model_gateway.py`
- `src/cnlc_agent/reports/assembler.py`
- PostgreSQL / Redis 核心实现

如发现必须跨边界修改，先记录 Integration Dependency / Architecture Issue，不擅自扩大任务。

## 完成后

提交并 push 到：

`origin/codex/demo-2026-09-22-web`

不要合并到 main，不要自动开始其他任务。

最后报告：

1. 实现内容
2. 新增/修改文件
3. AgentScope 版本
4. Agent Service 启动命令
5. 官方 Web UI 启动方法
6. Tool Call 演示路径
7. pytest / ruff / mypy / build
8. Integration Dependency
9. Architecture Issue

无架构问题时输出：

`No Architecture Issue found.`
