# docs/evidence｜事实、实验与决策证据

本目录不定义产品目标，而是保存支撑架构决策的证据。

当前包括：

- 01：现状事实盘点；
- 02：现有系统运行证据矩阵；
- 03：AgentScope 原生能力最小对照实验。

## 使用规则

架构决策必须区分：

- **已验证事实**；
- **实验结果**；
- **设计推论**；
- **待验证假设**。

只有证据足够时，才能把技术判断从 VERIFY 升级为 KEEP / REPLACE / WRAP / ADD。

Task 012A 当前 POC 的代码与结果位于：

- `experiments/agentscope_native_poc/`
- `artifacts/native_poc/`
- `tests/experiments/`

后续实验结果应优先回填本目录或对应 Task 证据，再修订 `docs/design/04` 与 `docs/design/05`。