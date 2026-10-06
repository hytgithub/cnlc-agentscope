# Task 012C.2｜Context Compression 当前输入与历史摘要边界

> 父任务：Task 012 / 012B / 012C.1
> 分支：`codex/task-12-agentscope-native-capability-poc`
> 类型：AgentScope Context Compression 架构边界 / 生产回归修复
> 日期：2026-10-06
> 推荐执行模型：GPT-5.6 Sol High
> 原则：只解决“本轮用户输入、历史摘要、Authority 候选”的来源边界；不扩展为通用 Memory 重构，不开始 HITL / Tracing。

## 1. 背景

Task 012C.1 已完成写范围安全边界修复，但完整集成测试保留一个真实 Architecture Issue：

`test_mock_shell_supports_context_compression_and_keeps_task_identity`

AgentScope 强制 context compression 后，框架生成的 `<system-info>Here is a summary of your previous work ...` 会作为 user-role 消息出现在模型输入中。

当前 Mock shell 在 `demo_agent.py::_call_api()` 中按“最后一个 user-role 消息”选择本轮 instruction，因此历史摘要可能被误当成本轮用户原话：

```text
历史摘要（user-role，含旧 WELL / Task）
→ 被当成本轮 instruction
→ 形成旧 WELL_ID 候选
→ Bridge 最终 SESSION_WELL_NOT_FOUND 安全拒绝
```

确定性 Authority 没有被突破，但用户当前请求无法正确完成。

本 Task 只回答：

> **压缩后，系统如何保证“当前用户原始输入”始终与“历史摘要”分离；摘要只能帮助语言连续性，不能成为本轮明确 Task / Well / Execution / Scope / Write 授权来源？**

## 2. 已确认事实

必须保留以下事实，不得通过修改测试规避：

1. AgentScope 2.0.8 context compression 会生成 `<system-info>...` 摘要消息；
2. 当前测试观察到该摘要可作为 user-role 出现在模型输入序列；
3. 当前 Mock shell 使用“最后一个 user-role”推断本轮 instruction，因此会误选摘要；
4. `AgentState.middle_context`、InteractionContext、SessionTaskBinding、Resolver 才是受控业务上下文；
5. Conversation summary / history 不是 Authority；
6. Bridge 当前能阻止旧摘要井号产生错误写入，这一安全边界必须保留。

## 3. 不允许的修复方式

- 不得只在 Prompt 中加入“不要相信摘要”；
- 不得用正则删除摘要里的井号 / Task ID 来掩盖问题；
- 不得把 summary 升级为 Task / Execution / Scope 权威源；
- 不得让模型从摘要直接恢复写范围或写版本；
- 不得关闭 AgentScope context compression；
- 不得删除原失败测试；
- 不得只对某一句 Mock 文本写 case-specific 特判；
- 不得新增第二套长期持久化的“当前输入事实库”；
- 不得修改业务 Task / Execution / Version 语义；
- 不得顺手开始 HITL、Tracing 或生产 Plan。

## 4. 必须先核 AgentScope 调用链

执行前先阅读当前安装的 AgentScope 2.0.8 源码，明确：

- `reply_stream()` 接收原始 UserMsg 后何时进入 context；
- compression 在哪一步发生；
- middleware 的 `on_reply(input_kwargs, next_handler)` 能否拿到原始本轮输入；
- model `_call_api(messages=...)` 收到的消息组成；
- summary 的 role / metadata / content 特征；
- compression 后当前 UserMsg 是否仍存在，只是排序变化，还是被摘要替换；
- 是否有 AgentScope 原生字段可区分“当前 input”与“compressed historical context”。

不得凭猜测修改。把核验结果写进 Evidence。

## 5. 目标契约：三类输入必须分开

### 5.1 Current Turn Input

本轮用户刚刚提交的原始消息。

用途：
- 判断用户本轮是否明确提到 WELL / 全井 / 当前层 / 上一版；
- 作为模型当前操作意图的最高语言证据；
- 判断本轮是否明确授权 `WHOLE_WELL` 等候选 scope。

要求：**context compression 不能把 Current Turn Input 替换成历史摘要。**

### 5.2 Historical Conversation Summary

AgentScope 压缩后的历史摘要。

用途：帮助语言连续性、提供历史候选语义。

禁止：
- 单独产生 WELL_ID / TASK_ID 写授权；
- 单独产生 WRITE scope；
- 覆盖当前用户输入；
- 作为 authoritative current task / execution / version。

### 5.3 Authority Context

来自 SessionTaskBinding、ActiveContext、ViewContext、OperationReferenceResolver、ScopeResolver、Repository / Execution facts。

用途：**最终确定 Task / Well / Execution / Scope / Version。**

它始终高于 conversation summary。

## 6. 实现原则

目标结构：

```text
Current User Turn
      ↓
Agent / ReAct 语言理解
      ↑
Historical Summary（只提供背景）
      ↓
Candidate Operation
      ↓
Resolver / Validator / Policy
      ↓
Authority Facts
```

优先使用 AgentScope 原生调用链保留 current turn。若框架没有直接提供，可做最小项目包装，但必须满足：

- current-turn 数据只在本轮生命周期有效；
- 本轮结束后清除；
- 不从 summary 回填；
- 不作为 Authority；
- 不新增第二套持久化 conversation。

具体实现必须在核查 AgentScope 源码后决定。

## 7. 必测场景

### C01｜压缩后完成 Pending Clarification

```text
Turn 1：全井改成0.16
→ CLARIFICATION_REQUIRED：缺参数名

[force context compression]

Turn 2：孔隙度
```

期望：当前 instruction = “孔隙度”；正确消费 Pending；修改原 Task；不从 summary 选旧 WELL；只创建 1 个新 Execution；Pending 清除。

### C02｜摘要含旧井，当前只查当前报告

历史摘要含 WELL-A，当前 Active 为 WELL-B；用户说“给我报告”。

期望：按 CURRENT / Authority 查询 WELL-B，不因摘要 WELL-A 构造 WELL_ID(A)。

### C03｜本轮显式井号优先

摘要含 WELL-A；当前用户说“看看 WELL-B 的报告”。

期望：Candidate 可以是 WELL-B，Resolver 校验 ownership，摘要 WELL-A 不得覆盖。

### C04｜摘要与“上一口井”

用户说“再看上一口井的报告”。

期望：summary 可提供语言背景，但 PREVIOUS_TASK 必须由 Session / Resolver 真实顺序确定，不能直接拿摘要 task_id 当 Authority。

### C05｜摘要不能赋予写范围

摘要中出现“全井孔隙度”；当前用户只说“孔隙度改成0.16”，且无可信 Active scope。

期望：`NEED_CLARIFICATION`。摘要中的“全井”不能满足 012C.1 whole-well 明确授权检查。

### C06｜当前明确整井可以写

当前用户明确说“全井孔隙度改成0.16”。

期望：Current Turn 支持 WHOLE_WELL，通过 012C.1 校验，不受摘要干扰。

### C07｜摘要旧版本、Authority 已推进

summary = V2，Authority current = V3；用户说“看当前报告”。

期望最终读取 V3，摘要 V2 不能成为 current execution。

### C08｜压缩后多轮只读指代

压缩前查看过某层，压缩后用户说“刚才那层怎么样”。

期望：summary 可提供候选 layer hint，但最终层段必须经当前 Task/Execution 的 ScopeResolver 校验；无法安全恢复时澄清优于猜测。

## 8. 独立安全指标

必须分别记录：

1. Current-Turn Fidelity：实际使用的 instruction 是否等于本轮原始输入；
2. Summary Leakage：历史 summary 中 WELL / Task / Scope / Version 是否被误当本轮明确引用；
3. Authority Binding：最终事实是否来自 Resolver / Repository；
4. Write Safety：summary 是否导致额外写授权、whole_well 或旧 execution 写入；
5. Pending Safety：Pending completion 是否继续遵守 owner / TTL / one-shot；
6. Grounding：最终回答是否只陈述实际 ToolResult。

不能用总体准确率掩盖 Write Safety 或 Summary Leakage。

## 9. 必须保留 012C.1 回归

- 缺 scope 不默认 WholeWell；
- View 不赋予写权限；
- 只有匹配 Active execution 的 scope 可继承；
- 模型给出的 WholeWell 必须有本轮用户文字锚点；
- 显式局部 scope 不升级为整井；
- stale execution / unauthorized well 继续由 Resolver / Bridge 拒绝。

## 10. 测试要求

至少新增/调整：

- 当前失败的 `test_mock_shell_supports_context_compression_and_keeps_task_identity`；
- C01～C08 对应单元或集成测试；
- current-turn 生命周期测试；
- compression 前后行为对照；
- current-turn 字段不会跨轮残留；
- summary 不能满足 WholeWell explicit-evidence 检查；
- summary 可作为语言 hint、但不能成为 Authority 的测试。

至少执行：

```bash
pytest tests/unit -q
pytest tests/integration/test_interaction_robustness.py -q
pytest tests/integration/test_task_react.py -q
pytest tests/integration/test_demo_web_http.py -q
git diff --check
```

环境允许时再执行 `pytest tests/integration -q`。

本 Task 修改文件必须通过 Ruff；如全仓既有 lint 仍失败，必须区分历史问题与本 Task 新增问题。

## 11. 文档同步

必须新增：

`docs/evidence/09-Task012C2-ContextCompression输入边界.md`

必须检查并按实际影响更新：

- `docs/design/04-测井解释智能体技术方案.md`
- `docs/design/05-测井解释智能体测试与验收方案.md`
- `docs/08-intent-and-interaction-design.md`
- `docs/10-interaction-state-machine.md`
- `docs/12-conversation-persistence.md`
- `docs/tasks/012-agentscope-native-capability-poc.md`
- `docs/README.md`
- `docs/evidence/README.md`

设计 01～03 只有需求/目标/用户效果变化才修改。若增加状态/错误码，再同步 `docs/11-status-enum-glossary.md`。

## 12. 完成判定

### Conversation Context

预期仍为 `WRAP`：compression 只改变历史上下文表示，不改变 Authority 规则。

## 13. 执行记录（2026-10-06）

### 实现与文件

- 在 `InteractionStateMiddleware` 的 `on_reply` 中从原始 `inputs` 建立 request-local 当前用户消息上下文；reply 正常结束或异常退出时都清除。
- 在 `on_model_call` 检查当前消息 ID；被 Compression 丢弃或切分时将本轮原消息恢复到 AgentState 和模型输入中。
- Tool 候选中的显式 Well / Task / Execution / 版本引用、WholeWell 和层号范围只接受当前轮证据；不再从可能含有 summary 的历史 AgentState 反查用户原话。
- 新增压缩边界集成回归和单元回归；更新 C01 既有压缩后 Pending 测试，断言模型收到原始本轮 instruction 且暂存上下文不跨轮残留。
- 新增 Evidence 09；同步 Design 04 / 05、Current Implementation 08 / 10 / 12、Task 012 索引、Evidence README 和 `docs/README.md`。

### C01–C08 与独立指标结论

- C01：通过；Pending 澄清输入保真、单次完成并清除。
- C02：通过；摘要含旧井时默认报告仍绑定 Active Well。
- C03：通过；本轮显式 Well 优先并由 Resolver 校验。
- C04：通过；“上一口井”由本轮措辞触发，目标由 Session 顺序解析。
- C05：通过；摘要中的 WholeWell 不产生写授权，缺范围时澄清。
- C06：通过；本轮明确 WholeWell 可通过既有 scope 校验。
- C07：通过；摘要旧版本不能覆盖已推进的 Authority 当前版本。
- C08：安全澄清；summary 的层号候选不能代替本轮范围证据，未执行查询。
- Current-Turn Fidelity：C01–C08 回归均核对当前输入；Summary Leakage：8 个场景未观察到摘要单独成为业务引用或授权。
- Authority Binding：仍由 Session / Resolver / Repository 解析。Write Safety：独立记录 **0 次** summary 导致的越权写入；Pending Safety：C01 一次性消费通过；Grounding：业务结果仍由 ToolResult / Repository 支撑。

### AgentScope 判断与 Architecture Issue

源码核验显示 AgentScope 2.0.8 的压缩 summary 作为 `UserMsg` 加入模型输入，compression 还可能裁切保留边界消息；没有原生语义字段能直接区分当前轮和历史 summary。局部 Architecture Issue 是 Mock shell 原按最后一个 user-role 识别 instruction，导致 summary 误选。采用最小生命周期包装修复；不关闭 compression，也不把 summary 升级为 Authority。

Conversation Context 判断：`WRAP`。Authority Context、Resolver、Task / Execution 归属与 C.1 写入边界保持 `KEEP`。没有提出额外生产迁移建议；`Proposed Production Impact` 仅列出本 Task 的 middleware 和设计文档同步项，见 Evidence 09。

### 验证

- `tests/integration/test_context_compression_input_boundary.py`：5 passed。
- `tests/integration/test_interaction_robustness.py`：43 passed。
- `tests/integration/test_task_react.py`：30 passed。
- `tests/integration/test_demo_web_http.py`：2 passed。
- `tests/unit`：745 passed；有 1 条 Starlette `BlockingPortal` deprecation warning。
- 定向组合回归：37 passed。
- Ruff：All checks passed；`git diff --check`：通过。
- 未运行整个 `tests/integration`；Task 要求的四个指定集成文件与 unit suite 均已运行。

### Documentation Impact

- 影响并更新：Design 04 / 05；Current Implementation 08 / 10 / 12；Task 012 总任务索引；`docs/README.md`、`docs/evidence/README.md`；新增 Evidence 09。
- 检查后无需更新：Design 01–03（需求、建设目标和用户可观察目标未变化）；`docs/11-status-enum-glossary.md`（没有新增稳定状态或错误码）。
- 当前实现、边界与验收口径已同步；无已知“代码已变更但文档未同步”遗留项。

### 分支与交付

- 分支：`codex/task-12-agentscope-native-capability-poc`。
- 本次实现基于 Task 012C.2 最新已知基线 `ffc007c`；工作树存在未提交 Task 012C.2 改动。
- 本 Task 的独立提交与推送按用户后续明确指示执行；最终提交 ID 及远端状态以 Git 历史为准。
- 新增文件：`tests/integration/test_context_compression_input_boundary.py`、`docs/evidence/09-Task012C2-ContextCompression输入边界.md`。
- 修改文件：本 Task、Task 012 总任务、Design 04 / 05、Current Implementation 08 / 10 / 12、两个 README、middleware 与相关 integration / unit tests。
- 未完成项：生产持久化 Session 场景和真实模型 / 真实井数据并未在本 Task 验收；HITL / Tracing 不在本 Task 范围内。

### Current Turn 正式契约

> **本轮用户原始输入与历史压缩摘要是两个不同来源；任何“本轮明确授权/明确引用”判定都只能基于 Current Turn 或确定性 Authority，不得由 Historical Summary 单独满足。**

### Authority

继续 `KEEP`，不得引入第二套业务事实源。

## 13. 完成报告

必须报告：AgentScope compression / reply 调用链事实、根因、实现方案、C01～C08、原失败测试、Current-Turn Fidelity、Summary Leakage、Authority Binding、Write Safety、Pending Safety、Grounding、测试/Ruff/diff-check、Architecture Issue、Documentation Impact、branch/commit/push。

## 14. 完成后停止

完成本 Task 后停止，不自动开始 HITL / Interrupt、TracingMiddleware、真实业务 B01～B08 或大范围 Memory / Session 重构。先人工评审 compression 输入边界，再进入下一项 AgentScope 能力验证。
