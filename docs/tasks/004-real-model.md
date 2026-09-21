# Task 04：公网真实模型接入

状态：已实施并通过离线验证。
工作分支：codex/task-04-real-model。

## 目标

在现有 ModelGateway Protocol 和 MockModelGateway 基础上增加真实 OpenAI-Compatible 实现，并接入北京区域 DashScope qwen-plus。

北京共享兼容地址：

https://dashscope.aliyuncs.com/compatible-mode/v1

凭据只能从本地环境读取，不得提交到 Git、文档、测试夹具或日志。

## 当前已有

- ModelRequest
- ModelGateway Protocol
- MockModelGateway
- model_base_url / model_api_key / model_name 配置字段

当前 bootstrap 仍固定使用 MockModelGateway。

## 实现要求

1. 增加通用 OpenAICompatibleModelGateway。
2. 异步调用 OpenAI-Compatible Chat Completions。
3. 模型地址、模型名、鉴权信息均从配置读取。
4. 增加 provider 选择，默认仍为 mock；真实 provider 配置缺失时明确失败，不允许静默回退 mock。
5. bootstrap 根据 provider 注入 Mock 或真实 ModelGateway。
6. ModelRequest 只做最小通用 prompt 映射，不设计完整测井专业 Prompt。
7. generate() 保持返回 JsonObject。
8. 模型返回必须解析并校验为 JSON object；非法输出转为 ModelError。
9. 对鉴权失败、超时、限流、服务端异常、非法响应、未知请求异常做可区分错误映射。
10. 可实现有限传输层 retry，但不得实现 Workflow Rollback 或无限重试。
11. 不允许 Agent 直接创建模型 SDK Client。
12. 不允许日志输出鉴权凭据。

## 建议配置

- model_provider
- model_base_url
- model_name
- model_timeout_seconds
- model_max_retries
- 鉴权凭据：继续使用现有本地环境配置能力，不写真实值。

## 依赖

优先使用成熟的 OpenAI-Compatible Python 客户端。
不要引入 DashScope 专有 SDK，除非存在明确技术原因。
目标是以后同一 Adapter 也可以复用于 vLLM 等兼容服务。

## 测试

单元测试至少覆盖：

- mock provider 保持可用；
- real provider 缺配置失败；
- 正常 JSON object 响应；
- 非 JSON；
- JSON 顶层非 object；
- 鉴权失败；
- timeout；
- rate limit；
- 5xx；
- 未知 transport error；
- 错误信息不泄漏鉴权凭据。

真实公网测试必须显式 opt-in，默认跳过。
真实测试只发送不包含井数据的简单 JSON 请求，用于验证 qwen-plus 连通性和 JSON object 返回。

## 不在本任务实现

- AgentScope Runtime
- 完整测井专业 Prompt
- W01-W10 业务扩展
- 新 Agent
- Web
- OpenTelemetry backend
- Workflow Rollback
- RAG
- 正式井数据专业 Schema

## 验收标准

- 默认 mock 全离线测试可运行；
- 可配置切换到真实 DashScope；
- 不允许真实 provider 缺配置时静默降级；
- qwen-plus 与北京兼容地址均通过配置接入；
- ModelGateway 仍是上层统一入口；
- 模型响应经过 JSON object 校验；
- 模型异常映射为项目 ModelError；
- pytest、ruff、mypy、build 通过；
- 提供显式 opt-in 的真实公网集成测试。

完成后按 AGENTS.md 报告实现、文件、配置、测试、真实公网测试结果、未完成事项和 Architecture Issue。

完成后不要自动开始 Task 05。

## 实施记录（2026-09-21）

### 实现内容

- 新增 `OpenAICompatibleModelGateway`，通过异步 `httpx` 调用
  `/chat/completions`，兼容 DashScope 北京地址以及其他 OpenAI-Compatible 服务。
- `generate()` 将 `ModelRequest` 的用途和 JSON context 映射为最小通用请求，要求
  `response_format=json_object`，只返回校验过的 JSON object。
- 增加鉴权失败、限流、超时、5xx、请求失败、非法响应和未知传输错误的稳定
  `ModelError` code；仅对限流、超时、网络错误和 5xx 做有限传输重试。
- `bootstrap.py` 按 `CNLC_MODEL_PROVIDER` 选择 `mock` 或
  `openai_compatible`/`real`，真实配置缺失时直接失败，不回退到 Mock。
- 模型 HTTP Client 由 Gateway 持有和异步关闭；上层 Agent 仍只依赖
  `ModelGateway` Protocol。
- 使用 `SecretStr` 和通用错误信息，日志只记录 task/trace/retry/error code，不记录鉴权值或供应商响应正文。

### 新增文件

- `src/cnlc_agent/infrastructure/model_gateway.py`
- `src/cnlc_agent/infrastructure/model.py`（兼容导出）
- `tests/unit/test_model_gateway.py`
- `tests/integration/test_real_model.py`（显式 opt-in）

### 修改文件

- `src/cnlc_agent/config/settings.py`
- `src/cnlc_agent/application/bootstrap.py`
- `src/cnlc_agent/application/runtime.py`
- `src/cnlc_agent/application/service.py`
- `src/cnlc_agent/infrastructure/__init__.py`
- `.env.example`、`pyproject.toml`、`uv.lock`

### 配置项

```dotenv
CNLC_MODEL_PROVIDER=mock
CNLC_MODEL_TIMEOUT_SECONDS=30
CNLC_MODEL_MAX_RETRIES=2
CNLC_MODEL_RETRY_BACKOFF_SECONDS=0.25
MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MODEL_NAME=qwen-plus
MODEL_API_KEY=
# 也支持 DASHSCOPE_API_KEY
```

默认 provider 为 `mock`，不依赖公网。将 `CNLC_MODEL_PROVIDER` 设置为
`openai_compatible`（或 `real`）后，`MODEL_BASE_URL`、`MODEL_NAME` 和
`MODEL_API_KEY`/`DASHSCOPE_API_KEY` 缺一即报配置错误。

### 测试结果

- `uv run pytest -q`：**63 passed, 4 skipped**；其中 1 个公网模型测试因未配置显式 opt-in 环境跳过，3 个 Task 03 真实服务测试因环境变量缺失跳过。
- `uv run pytest tests/unit/test_model_gateway.py -q`：**14 passed**。
- `uv run ruff check .`：通过。
- `uv run ruff format --check .`：通过。
- `uv run mypy`：36 个源码文件通过。
- `uv build`：通过，生成 sdist 和 wheel。

真实公网测试：本机未设置 `CNLC_RUN_REAL_MODEL_TEST=1`、`MODEL_API_KEY` 或
`DASHSCOPE_API_KEY`，因此按“显式 opt-in”规则跳过；未伪造公网通过结果。
配置这些变量后可运行：

```bash
CNLC_RUN_REAL_MODEL_TEST=1 uv run pytest tests/integration/test_real_model.py -v
```

### 未完成 / TODO

- 公网真实模型联调需由提供凭据的运行环境执行；凭据不进入仓库。
- 真实模型调用目前只提供通用 JSON Gateway，专业 Prompt、正式井数据 Schema 和
  专业算法不在本 Task 范围。
- Task 05 的 AgentScope Runtime、Web、RAG 和专业业务扩展不在本 Task 实施。

### Architecture Issue

**No Architecture Issue found.** 本次只增加 ModelGateway Adapter 和配置选择，未新增 Agent、未修改 W01–W10、未替换数据库/Redis，也未把确定性测井算法交给模型。
