# Task 10.5-E2.1：多轮澄清连续性与数值来源校验

## 背景与范围

- 开始 HEAD：`4da5c7a43b91e42a3cb0ef4513aee7c084d41a09`。
- 开发分支：`codex/task-10-5-operation-understanding`。
- E2 已完成 ReAct → Operation Tool → Resolver / Validator / Bridge → TaskCommands 主链。
- 本次只修复多槽位逐轮补齐，以及模型关键数字必须来自当前用户消息的安全边界。

ClarificationSlot（澄清槽位）和 Missing Field（缺失字段）只检查结构完整性，绝不用于中文
自然语言意图识别。正式链路仍是 `User text → qwen-plus → Tool Schema → structured semantics`。
规则代码不判断“孔隙度”“改一下”“第 6 层”的语言含义；模型先形成结构化字段，后端再检查
字段能否修补、数字是否来自本轮、引用是否可信及 Capability 是否允许执行。

## 多轮 Pending 生命周期

新 PLAN 缺槽后产生 P1。下一轮合法 ClarificationPatch 原子消费 P1 并形成更新后的
PartialOperationPlan；Bridge 再次执行完整 Resolver 和 Validator。如果仍为
`NEED_CLARIFICATION`（需要澄清），Controller 使用这个更新后的计划、本轮重新核验的
Task / Execution / Scope 和剩余 issues 创建 P2。P2 的 `created_turn` 与 TTL 从当前有效补槽轮
重新开始。后续可按相同方式形成 P3；计划完整且全部校验通过时只提交一次业务 Command，
提交前消费 Pending，重复 Patch 返回 `CLARIFICATION_SLOT_INVALID`（澄清槽位修补非法）。

新的 Pending 来自服务端成功 patch 后的计划，不从聊天记录重建，也不允许模型重交历史计划。
首轮已锁定的 Task / Execution 会在每轮重新解析后重建锁；即使 Active 被切到另一 Task，
最终也只能作用原 Task，或者因引用失效而安全失败。Correction（修正）更换层号后如果仍缺值，
同样创建下一轮 Pending；最终局部能力未开放时返回已识别但不支持，绝不扩大为整井操作。

`CANCEL`（取消）立即清除整个链。无关轮次、owner 改变、TTL 到期和 Redis miss 仍清除，
PostgreSQL durable restore 不恢复 Pending。新 PLAN 明确替换旧 Pending。

## 当前轮数字来源校验

`extract_grounded_numbers()` 是唯一数字提取函数，PLAN 与 CLARIFICATION_REPLY 共用。
它只读取当前 UserMsg 的阿拉伯数字，百分数按绝对小数折算：`16%` 可匹配 ABSOLUTE 0.16，
不能匹配 ABSOLUTE 16 或 0.18。DELTA / PERCENT_CHANGE 仍由既有 Bridge 拒绝，本次没有开放。

校验对象包括：

- PLAN 和 ClarificationPatch 中真正可能写入的 ABSOLUTE 数值；
- `INTERVAL_ORDINAL`（单层号引用）的 ordinal；
- `MULTI_INTERVAL_ORDINAL`（多层号引用）的每一个 ordinal。

服务器不判断数字属于哪个专业字段，不对 task_id / execution_id 做文本匹配，不扩展
prediction_model 等字符串 grounding，也不引入中文数字 NLP。用户写“第六层”而模型输出 6
时保持安全拒绝，需用阿拉伯数字重述。

Grounding mismatch 返回 `INVALID_OPERATION_PLAN`（操作计划结构非法）、0 新 Execution。
已有 Pending 会以原服务端计划和可信锁续留到当前轮，使用户可以下一轮重新回答。AgentScope
在 Middleware 前拒绝 malformed Clarification Reply 时走同一保留逻辑。明确的新 PLAN、CANCEL
或 SET_ACTIVE_CONTEXT 仍终止旧 Pending。合法但越权的 ClarificationPatch 也保留原计划。

## 测试与验收

确定性测试覆盖：

- 缺 TARGET + VALUE：第二轮补 TARGET 后仍存在只缺 VALUE 的新 Pending，第三轮补 0.16
  只创建一个 Execution；重复补值不再次执行。
- 缺 TARGET + VALUE + PERSIST_MODE：逐槽形成 P1 / P2 / P3，最后一轮才执行一次。
- 首轮锁定 Task A / V1，第二轮后 Active 切到 B，第三轮仍只修改 A。
- 第 5 层改为第 6 层的 Correction 后继续等待 VALUE；补值后局部能力安全拒绝，0 Execution。
- 续存后 CANCEL，再补值返回 CLARIFICATION_SLOT_INVALID，0 Execution。
- value 0.16、百分数 16%、ordinal 6、多 ordinal 3/5/7 的匹配与不匹配组合。
- grounding mismatch 与 malformed Patch 后 Pending 内容及锁保持，下一轮正确重答可以完成。
- Scripted ReAct 按“帮我改一下 → 孔隙度 → 0.16”走 Middleware 与统一 Tool，exactly once。
- Conversation persistence 回归验证 Pending 不进入 PostgreSQL durable restore，Redis miss 不恢复。

真实页面使用独立新会话先建立 `WELL_MOCK_001` 的 Execution #1，再完成两条 smoke：

- `帮我改一下 → 孔隙度 → 0.16`：前两轮右侧仍只有 #1，分别提示补 TARGET 和 VALUE；
  第三轮新增且只新增 Execution #2，报告显示孔隙度 0.16。
- `帮我改一下 → 孔隙度 → 算了 → 0.16`：取消后数值输入形成新的缺 TARGET 计划，右侧始终
  只有 #1、#2，没有恢复旧 Pending 或新增 Execution。

前端代码未修改。真实 qwen-plus smoke 作为模型兼容验证，核心正确性由确定性测试保证。

| 检查 | 结果 |
| --- | --- |
| `pytest -q tests/unit/` | 647 passed。 |
| 四组核心 interaction / ReAct / HTTP integration | 72 passed；Starlette 第三方弃用告警 1 条。 |
| Conversation / Execution Bridge PostgreSQL、Redis persistence | 3 passed。 |
| 真实 qwen-plus ReAct smoke | 1 passed；三轮连续澄清仅产生一个 Execution。 |
| 两条真实页面 smoke | 通过。 |
| Ruff、mypy、`git diff --check` | 通过。 |

## 未实现与架构检查

本次未开放局部重跑、RECALCULATE、REINTERPRET、COMPARE、SCENARIO、历史版本分支写，
未修改 W01-W10、数据库 Schema 或 migration，未增加 Agent、Tool、request mode、枚举或稳定
错误码，因此不更新 glossary。

No Architecture Issue found.
