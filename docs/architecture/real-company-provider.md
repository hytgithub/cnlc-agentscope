# 真实公司 Provider 接入与规范化

## 1. 当前完成度

Task 11F 实现 `company_real`（真实公司 Provider）传输和规范化边界：

```text
CompanyInputResolver
        ↓
RealCompanyBatchProvider
        ↓
CompanyApiClient
        ↓
CompanyProviderCall（可恢复持久事实）
        ↓
CompanyResultTool（DERIVED）
        ↓
W03～W08 当前可识别字段
```

已实现：

- 使用现有真实 HTTP 协议的上传、预处理、下载和同步预测 Adapter；
- 真实预处理与预测响应的受控规范化；
- HTTP、鉴权、业务错误、超时和非法响应的安全失败；
- ProviderCall PostgreSQL/InMemory 持久化与跨进程恢复；
- `httpx.MockTransport` 协议验证；
- REAL（真实物理调用）与 DERIVED（派生业务结果）ToolRun 关联。

未完成：

- 浏览器 GDSX 上传及 GDSX → WellData 正式解编入口；
- 大文件 Artifact Store（对象存储）；
- 公司内网真实联调和压力测试；
- 预测字段到最终专业结论的业务验收。

因此 `company_real` 表示 transport/normalization ready，不表示可以从 Web GDSX 上传完整运行
W01～W10。GDSX task ingress 和大结果 Artifact 由 11G 承接。

## 2. 能力与业务阶段边界

| Provider Operation | 当前真实能力 | Business Stage | 处理 |
| --- | --- | --- | --- |
| `analysis` | 没有已确认 API | DECODE | `COMPANY_OPERATION_NOT_IMPLEMENTED`（真实操作未实现） |
| `preprocessing` | `CompanyApiClient.preprocess()` | PREPROCESS / W03 | 上传 GDSX、执行 `processForGDSX`、验证下载文件并持久化预测输入 |
| `interpretation` | `CompanyApiClient.predict()` | INTERPRET / W04～W08 | 使用同 Execution/InputVersion 的预处理结果调用同步预测并投影已确认字段 |
| `report` | 没有已确认 API | 无 | 不装配真实 Tool；W10 继续本地结构检查，业务 REPORT 仍由 `ReportAssembler` 生成 |

`analysis` 和 `report` 不调用 Mock，也没有 Real → Mock fallback。W01 在当前验证链路中仍由明确的
Fixture/Input adapter 提供；这是不同能力来源的显式组合，不是失败降级。

## 3. 输入解析边界

`CompanyInputResolver` 只通过 `task_id`、`execution_id`、`input_version_id` 解析：

- 基础设施内部 GDSX `Path`；
- 显式预处理操作；
- `RealPredictionContext`：`well_name`、`service_id`、`task_config`。

默认 `ConfiguredCompanyInputResolver` 只读取明确的 `CNLC_COMPANY_*` 配置，不读取聊天文本、
ActiveContext 或当前工作目录。测试使用临时 GDSX 和 Test Resolver。Path 只存在于基础设施适配器，
不会进入 InterpretationState、Execution、StageRun、DatasetRevision、ToolRun snapshot 或
CompanyProviderCall。

可选 `CompanyArtifactSink` 只接收处理后 GDSX bytes 并返回逻辑 `processed_artifact_ref`。当前未配置
Sink 时只验证下载确实是 GDSX/HDF5，不保存文件字节；配置测试 Sink 时也只持久化逻辑引用。

## 4. 真实预处理

真实预处理固定执行：

1. 上传显式 GDSX；
2. 使用显式 operations 调用 `processForGDSX`；
3. 获得后续预测需要的 `logReqJson`；
4. 下载并验证 GDSX/HDF5 magic bytes；
5. 将有界规范化事实写入 ProviderCall。

成功的 preprocessing `normalized_result` 包含：

- `provider_operation=preprocessing`；
- `external_call_id`；
- `input_version_id`；
- 预测必需的 `logReqJson`；
- `operation_keys`；
- 可选逻辑 `processed_artifact_ref`。

不保存 token、Authorization、服务端路径、本地 Path 或 GDSX bytes。W03 当前只确定性报告真实传输和
预处理操作已经完成；公司接口没有给出正式 QC 统计时明确给出 warning，不推算质量结论。

## 5. 真实预测与恢复

`interpretation` 查询同一 Task、Execution、InputVersion 下成功的 preprocessing ProviderCall，读取
持久 `logReqJson`，再与 Resolver 明确提供的 `wellName`、`serviceId`、`taskConfig` 组合调用
`encodingInferenceBySyn`。这些字段不会猜测或自动补齐。

PREPROCESS 暂停、服务重启和用户确认后，新进程通过 PostgreSQL ProviderCall 恢复 `logReqJson`，
不依赖 `CompanyBatchResults` 内存缓存或有损 ToolRun snapshot。V1 结果不能提供给 V2；InputVersion
不一致同样拒绝为 `COMPANY_RESULT_VERSION_MISMATCH`（公司结果版本不匹配）。

预测仍沿用 `project_prediction()` 已确认的字段集合：

- SAND / LIME / DOLO / CARB / ANHY；
- POR / PERM / SH；
- SW；
- JSJL；
- ogResultList。

投影只说明 Provider 返回相应字段。曲线存在不等于“物性好”或“油层”，结果保持
`REVIEW_REQUIRED`（需要人工复核）或 `BLOCKED`（缺失阻断），不增加分类阈值或地质推断。

W09 不使用不存在的公司 validation API，继续走现有 `ValidationAgent`。组合根按 Step capability
选择路径，不再用一个 `company_batches` 布尔值把 W03～W10 整体切换到公司批量能力。

## 6. CompanyProviderCall

ProviderCall 是真实物理调用的可恢复执行事实，不是版本实体，也不替代 ToolRun。字段为：

- `provider_call_id`（内部稳定身份）、`external_call_id`（ToolRun 关联身份）；
- `task_id`、`execution_id`、`input_version_id`；
- `provider_operation`、`status`；
- `request_summary`、`normalized_result`；
- `started_at`、`finished_at`、`error_code`。

真实物理调用开始前即生成并持久化 `provider_call_id` 与 `external_call_id`。状态为
`RUNNING`（调用中）、`SUCCESS`（调用成功）、`UNKNOWN`（结果未知）或 `FAILED`（调用失败）。数据库外键绑定 Task、
Execution 和 InputVersion；Repository 还验证 Execution 实际绑定同一 InputVersion。

同一 `task_id + execution_id + input_version_id + provider_operation` 只允许一个事实。`SUCCESS`
直接恢复持久结果；`RUNNING`、`UNKNOWN` 和 `FAILED` 都拒绝自动重发。本任务不实现自动 Retry，
后续人工核实入口可以直接查询该 ProviderCall。

`normalized_result` 最大 512 KiB，`request_summary` 最大 16 KiB，嵌套深度最大 10。模型拒绝非有限
数字、敏感键、本地/服务端绝对路径和不合法 JSON。超限返回
`PROVIDER_RESULT_TOO_LARGE`（规范化结果过大），不截断后继续作为专业结果；后续由 11G Artifact
Store 承接。失败 ProviderCall 只保存安全 error code，不保存 HTTP body 或异常正文。

## 7. ToolRun 与 Stage 展示

一次真实 provider batch 产生一个 `ToolExecutionMode.REAL`（真实执行）ToolRun；从该响应读取的
业务 Tool 产生 `DERIVED`（派生结果）ToolRun。两者共享 `source_external_call_id`，可以准确表示：

```text
一次真实 API 调用
        ↓
多个业务结果投影
```

ToolRun 仍只保存有界审计摘要。StageToolRunView / StageResultView 不读取 ProviderCall 的完整
normalized result，不展示 curveData、GDSX、凭据、路径或 HTTP 响应。业务 Stage 继续从 StepId
确定性推导，没有新增 stage 数据库字段。

## 8. 失败与重试

- 鉴权、业务失败、非法 JSON、空/多井预测响应记为 `FAILED`（调用失败）；
- HTTP 超时或外层 Tool timeout 取消记为 `UNKNOWN`（结果未知），因为不能确认服务端是否已执行；
- ProviderCall 与 ToolRun 同时记录失败事实；
- 不保存 token、Authorization、HTTP body、server path 或栈信息；
- 不进行 Real → Mock fallback；
- `SUCCESS` 恢复持久结果，`RUNNING` / `UNKNOWN` / `FAILED` 均不自动重新提交；
- 本任务不新增 Retry、Rollback、Trace、前端、Agent、Prompt 或专业算法。

No Architecture Issue found.
