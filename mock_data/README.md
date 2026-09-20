# Mock 井资料（Task 01）

`WELL_MOCK_001.json` 是开发者编写的虚构样例，不是项目方提供的真实井数据。
只用于验证工程骨架，所有专业参数和结论均为 fixture 预设，没有执行专业计算。

**Pending final well-data schema.** 当前 `schema_version` 为 `0.1-skeleton`。

| 字段 | 说明 |
|---|---|
| `well` | 最小井信息，井标识须与文件名相同 |
| `raw_data.depths` | 严格递增的 MD 深度，单位 m |
| `raw_data.curves` | 曲线名称、单位、与深度等长的采样数组，允许 null |
| `raw_data.auxiliary` | 演示用辅助证据，尚非正式专业 Schema |
| `requirements.required_curves` | 该演示输入声明的必要曲线；不代表正式测井标准 |
| `requirements.recommended_sources` | 缺少时产生 WARNING 的演示证据 |
| `outputs` | 各 Mock Tool / Model 返回的预设结果，必须 `is_mock=true` |
| `validation` | ValidationAgent 的预设独立验证结果 |
| `extensions` | 可扩展 JSON 数据；其他拼错的顶层字段会报错 |

`qc` 节点仅透传曲线并返回 Mock QC 结果，不执行环境校正。
`petrophysics` 与 `sw` 中的数据为预设值，不使用任何猜测的孔隙度、渗透率或 Sw 公式。
`intervals` 中的厚度也为预设值，正式有效厚度标准待业务确认。
专业 Agent 的响应由 `MockModelGateway` 回放，不会向外部模型发送井资料。

故障场景由测试在临时目录内修改此样例触发，不污染正常演示文件。
