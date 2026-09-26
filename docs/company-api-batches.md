# 公司能力四大步骤接入（当前 Mock 返回）

状态和 Tool 执行模式中文说明见 [11-status-enum-glossary.md](11-status-enum-glossary.md)。

实现位置是 `cnlc-agentscope`。`qwen-agent-main` 只用于查看调用协议，不需要启动。
本次保持 W01–W10 顺序和现有前端，增加四个批量能力入口；入口直接返回现有
`MockFixture` 或本次上传的 JSON 中相应结果，不访问公司服务。

## 启用与使用

项目根目录本地 `.env`：

```dotenv
CNLC_MODE=demo
CNLC_MODEL_PROVIDER=mock
CNLC_PROFESSIONAL_PROVIDER=company_mock
```

重启 Python 8000 后端后，在现有前端新建会话，上传 `mock_data/WELL_MOCK_001.json`，
输入“帮我解释这口井”。旧会话的历史报告不会自动重算。前端无需重新安装依赖。
保留原有数据库、Redis 配置；该功能没有新增服务依赖。
恢复原有逐工具路径可将 `CNLC_PROFESSIONAL_PROVIDER` 改为 `fixture`。

## 当前数据分配

| 批量入口 | 返回内容 | 现有步骤如何消费 |
| --- | --- | --- |
| analysis | well、raw_data、requirements | W01 加载；W02 根据返回的要求检查完整性 |
| preprocessing | qc、processed_data、operations_applied | W03 质量结果；Mock 未转换曲线，处理后数据沿用原数据 |
| interpretation | lithology、petrophysics、sw、fluid、classification、intervals、validation | W04–W09 分别提取对应结果；W06 同时消费 Sw 和流体结果 |
| report | final_check | W10 本地结构检查；随后沿用本项目报告模板生成报告 |

这些键是本项目内部规范化契约，**不是对公司 HTTP 响应结构的声明**。
`validation` 目前来自现有 Fixture，不代表公司已经提供综合验证 API。
没有找到独立的公司报告接口，因此报告仍由本项目生成。
当前仍接收现有 Mock JSON，不代表已完成真实 GDSX 上传解析与预测链路。

## 边界与追踪

- `tools/company_batches.py` 的 `MockCompanyBatchProvider.execute` 就是当前 return Mock 的位置。
- 同一次执行、相同输入版本和参数共享一份批量响应；不同执行或参数不会串用。
- 批量工具记录为 `MOCK`（模拟执行），消费结果的细分工具记录为 `DERIVED`（派生结果），共享 `external_call_id`；`DERIVED` 不代表又发起了一次独立公司 API 调用。
- 结果携带 `is_mock=true`。批量请求成功不代表所有细分结果成功；缺项、失败、
  必需曲线缺失和严重验证冲突继续触发失败、阻塞或人工复核。
- 工具超时沿用 `CNLC_TOOL_TIMEOUT_SECONDS`，当前不进行网络请求。
- 不使用大模型重新估算专业参数，也不补造分类阈值或深度单位。

## 真实接口准备与后续替换

`infrastructure/company_api.py` 根据参考代码整理了预处理上传/处理/下载、预测和
模型列表的 HTTP 客户端；`application/company_results.py` 只做原始预测字段分组。
两者**没有接入当前默认运行链路，也没有实际访问公司内网验证**。
原始真实结果只会标为待复核或缺数据，不直接宣称专业步骤完成。

待公司接口和真实样本可用后，在批量提供者内调用真实客户端，把返回内容适配为上述
内部字段；公司将大接口拆为细接口时，也在这里调整调用与映射，保持 W 步骤消费契约。
需要同时核对真实井标识、曲线深度/单位、分类编码、错误信封和鉴权，
再接入 GDSX 输入及明确非 Mock 来源。当前不能仅填写 Token 就切为完整真实模式。

## 验证

新增批量流程测试覆盖完整 W01–W10、共享调用来源、并发缓存、执行隔离、细分缺项、
Sw 失败、缺少必需曲线和验证冲突。网页上传测试同时覆盖 fixture/company_mock，
检查步骤进度、批量工具标签和最终报告流。HTTP 客户端测试使用 `httpx.MockTransport`，
不等同于公司服务联调。

本次没有新增核心 Agent、调整 W01–W10 顺序或改变持久化架构。
No Architecture Issue found.
