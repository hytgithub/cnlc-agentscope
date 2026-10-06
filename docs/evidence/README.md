# docs/evidence｜事实、实验与决策证据

本目录不定义产品目标，而是保存支撑架构决策的证据。

当前包括：

- 01：现状事实盘点；
- 02：现有系统运行证据矩阵；
- 03：AgentScope 原生能力最小对照实验设计；
- 04：Task 012A Prompt + Toolkit + Skill A/B 实验结果；
- 05：Task 012A.1 Skill 稳定性、重复运行与 Response Grounding 实验结果。
- 06：Task 012B 原始多轮 Context 与权威锚定结果，以及 Task 012B.1 补齐后的 5 次汇总证据；
- 07：Task 012C AgentScope 原生 Task Tools 多步骤 A/B 结果，含 60 次运行、离线复核指标及 P04 写安全失败。
- 08：Task 012C.1 写范围语义与生产安全边界审计，含 scope 来源矩阵、S01–S07 代码级核对和最小修复回归。

## 使用规则

架构决策必须区分：

- **已验证事实**；
- **实验结果**；
- **设计推论**；
- **待验证假设**。

只有证据足够时，才能把技术判断从 VERIFY 升级为 KEEP / REPLACE / WRAP / ADD。

Task 012A / 012A.1 / 012B 当前 POC 的代码与结果位于：

- `experiments/agentscope_native_poc/`
- `artifacts/native_poc/`
- `tests/experiments/`
- Task 012B 原始与补齐运行的记录及汇总位于 `experiments/agentscope_native_poc/artifacts/task012b*.jsonl` 与对应 summary 文件。

后续实验结果应优先回填本目录或对应 Task 证据，再修订 `docs/design/04` 与 `docs/design/05`。

Task 012B.1 已完成 M01–M06 重复补齐，并将 Evidence 06 回写到 `docs/design/04` 与 `docs/design/05`。Task 012C 已完成 Task/Plan 隔离 A/B 并将 Evidence 07 回写到主设计；不把隔离 POC 等同生产验收，也不自动授权生产迁移。
