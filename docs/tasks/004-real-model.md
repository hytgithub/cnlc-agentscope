# Task 04：公网真实模型接入

状态：待实施。
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
