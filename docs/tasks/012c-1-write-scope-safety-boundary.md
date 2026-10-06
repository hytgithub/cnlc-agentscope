# Task 012C.1｜写范围语义与生产安全边界核对

> 父任务：Task 012 / 012C
> 分支：`codex/task-12-agentscope-native-capability-poc`
> 类型：生产语义审计 / 安全边界收口 / 必要时最小修复
> 日期：2026-10-04
> 推荐执行模型：GPT-6 Luna
> 原则：先核对真实需求、当前实现和测试，再决定是否改代码；不得为了与 POC 对齐而直接修改生产语义。

## 1. 背景

Task 012C 已完成 AgentScope Task Tools A/B，对 Task/Plan 的当前结论为 `REGISTERED_BUT_UNUSED`。实验同时暴露 P04：用户没有给出修改范围时，模型可能自行补成 `whole_well` 并继续模拟写入。

012C 原实验只证明 POC 中存在模型写范围风险，不能单独证明生产存在同样缺陷。但后续人工代码复核发现生产当前实现确实存在一条需要审计的语义链：

```text
PartialOperation / OperationPlan 中 scope 缺失
        ↓
OperationExecutionBridge
        ↓
op.scope = op.scope or WholeWellScope()
        ↓
MODIFY_PARAMETER 当前 capability 仅允许 WHOLE_WELL
        ↓
无显式 scope 的 por / perm / sampling_interval / prediction_model 修改可继续执行
```

同时，当前 `missing_plan_slots()` 对 MODIFY_PARAMETER 会检查 target / value / model，但不会把 scope 缺失列为必须澄清；现有 `test_supported_parameters_really_create_one_execution` 也使用无 scope 的参数修改并期待 SUCCESS。

而主设计已经写明：

> 缺失或歧义必须澄清，禁止模型自行补全写范围。

测试方案 A10 也明确要求：

> 模型不得自行把缺失范围补成 `whole_well`。

因此目前存在一个**实现语义与最新设计边界可能不一致**的问题。

本 Task 不预设哪一边一定正确，而是回答：

> **当用户提出 MODIFY_PARAMETER 但没有明确 scope 时，正式产品应该默认整井、继承可信 Active scope，还是必须 NEED_CLARIFICATION？不同参数是否需要不同规则？**

## 2. 必须核对的事实

至少检查以下文件和真实代码，不得只依据本 Task 文字：

- `docs/design/01-测井解释智能体需求规格说明.md`
- `docs/design/03-测井解释智能体预期效果.md`
- `docs/design/04-测井解释智能体技术方案.md`
- `docs/design/05-测井解释智能体测试与验收方案.md`
- `docs/08-intent-and-interaction-design.md`
- `docs/10-interaction-state-machine.md`
- `src/cnlc_agent/demo/operation_parser.py`
- `src/cnlc_agent/demo/operation_context_resolver.py`
- `src/cnlc_agent/demo/operation_execution_bridge.py`
- `src/cnlc_agent/demo/operation_capabilities.py`
- `src/cnlc_agent/demo/plan_validator.py`
- `src/cnlc_agent/demo/interaction_context.py`
- `src/cnlc_agent/demo/interaction_state.py`
- `src/cnlc_agent/demo/operation_command_adapter.py`
- `tests/unit/test_operation_execution_bridge.py`
- 与 MODIFY / scope / ActiveContext 相关的集成测试。

## 3. 必须区分三种 scope 来源

不能把所有 scope 都视为“模型传入的 scope”。至少分开：

### A. 用户本轮明确范围

例如：

```text
把全井孔隙度改成 0.16
```

或：

```text
把 2035–2038m 这层孔隙度改成 0.16
```

### B. 可信交互上下文继承

例如 ActiveContext / ViewContext 已经有明确 scope，并且当前操作语义允许继承。

必须检查当前代码中：

```python
if op.scope is None:
    op.scope = active.scope
```

这类逻辑的实际条件、权限和版本约束。

### C. 完全缺失范围

用户只说：

```text
把孔隙度改成 0.16
```

且没有可信 Active scope 可以合法继承。

本 Task 的核心就是确定 C 应如何处理。

## 4. 参数粒度不能一刀切

当前支持：

- `por`
- `perm`
- `sampling_interval`
- `prediction_model`

必须分别判断当前产品语义。

至少形成以下矩阵：

| 参数 | 当前实现粒度 | 业务上是否天然整井 | 缺 scope 当前行为 | 目标行为 | 依据 |
|---|---|---|---|---|---|
| por | 待核 | 待确认 | 待核 | 待定 | 代码/需求 |
| perm | 待核 | 待确认 | 待核 | 待定 | 代码/需求 |
| sampling_interval | 待核 | 待确认 | 待核 | 待定 | 代码/需求 |
| prediction_model | 待核 | 待确认 | 待核 | 待定 | 代码/需求 |

不得因为当前 capability 只支持 WHOLE_WELL，就倒推出“用户没说范围时一定默认 WHOLE_WELL”。

也不得因为设计 05 A10 写了禁止默认 whole_well，就忽略某些参数可能在产品上天然只有整井语义。

如果业务资料不足以确定，必须标成 `BUSINESS_CONFIRMATION_REQUIRED`，不能自行发明专业规则。

## 5. 必测场景

至少覆盖：

### S01｜明确整井

```text
把全井孔隙度改成 0.16
```

确认是否能明确产生 WHOLE_WELL。

### S02｜无范围

```text
把孔隙度改成 0.16
```

分别在：

- 无 Active scope；
- Active scope = WHOLE_WELL；
- Active scope = INTERVAL / DEPTH_RANGE；
- View 与 Active 不同；

场景下验证真实结果。

### S03｜明确局部范围

```text
把刚才这层孔隙度改成 0.16
```

如果当前 capability 不支持局部修改，应稳定返回 `UNSUPPORTED_OPERATION` 或项目约定的等价结果，不得静默扩大成整井。

### S04｜采样间隔

```text
把采样间隔改成 0.2m
```

判断该参数是否天然整井；如果是，必须有代码/需求/业务依据。

### S05｜预测模型

```text
切换到 model-v2
```

判断模型选择是否天然绑定整井 execution；如果是，同样必须有依据。

### S06｜多轮查看后修改

```text
Turn 1：看看 2035–2038m
Turn 2：把孔隙度改成 0.16
```

判断“查看范围”是否能成为可信修改范围。若正式规则未确认，不能默认允许。

### S07｜旧 View / 新 Active 冲突

View 指向历史版本局部层段，Active 指向当前写基线。

验证缺 scope 的修改不能因为旧 View 而错误写到历史或错误范围。

## 6. 审计结论必须先于代码修改

先输出一张正式决策表：

| 场景 | 当前行为 | 目标行为 | 是否一致 | 是否需要代码修改 |
|---|---|---|---|---|

并给出以下之一：

### 结论 A｜当前默认 WHOLE_WELL 是明确且被需求支持的产品语义

则：

- 不改代码；
- 修正 04/05 中过于绝对的“缺 scope 必须澄清”表述；
- 明确哪些参数可以省略 scope 以及为什么。

### 结论 B｜只有可信 Active scope 可以继承，完全缺失必须澄清

则：

- 保留受控 Active scope 继承；
- 禁止最终 fallback 到 WholeWellScope；
- 缺 scope 且无可信继承时返回 NEED_CLARIFICATION；
- 增加单元和集成测试。

### 结论 C｜任何写操作 scope 都必须用户本轮明确

则：

- 不允许 Active scope 自动继承；
- 需要更严格修改 Parser / Resolver / Bridge；
- 只有有明确需求依据才能采用这一结论。

### 结论 D｜不同参数规则不同

则必须将差异放入确定性参数能力契约，不能交给 Prompt 猜。

## 7. 如果需要代码修改

必须采用最小改动，优先在语义校验边界处理，而不是给 Prompt 增加一句规则。

候选修改点由审计结果决定，可能包括：

- `missing_plan_slots()`
- `PlanValidator`
- `OperationContextResolver`
- `OperationExecutionBridge`
- capability metadata / parameter policy

不得在多个层重复写同一规则。

原则：

> **范围合法性由确定性代码保障；Agent 只提供候选 scope。**

## 8. 回归测试要求

如果代码发生变化，至少覆盖：

- 显式 WHOLE_WELL 仍能成功；
- 缺 scope 的目标行为；
- Active scope 合法继承行为；
- View/Active 冲突；
- 局部 scope 不得扩大为 whole_well；
- por / perm / sampling_interval / prediction_model 各自规则；
- 多操作计划中缺 scope 不能因其他 operation 被补全；
- 写前 re-resolve 后行为一致；
- 现有 FULL_RERUN / REPORT / STATUS 不受影响。

至少执行：

```bash
pytest tests/unit -q
pytest tests/integration -q
ruff check src tests
git diff --check
```

如完整集成测试依赖本地 PostgreSQL / Redis，按项目已有测试约定执行并如实记录不可运行项。

## 9. 文档同步

无论最终是否改代码，都必须更新：

- 新增 `docs/evidence/08-Task012C1-写范围语义与安全边界核对.md`；
- `docs/tasks/012c-1-write-scope-safety-boundary.md` 执行结果。

根据结论检查并必要时更新：

- `docs/design/01`：只有需求语义变化才改；
- `docs/design/03`：只有用户可观察行为变化才改；
- `docs/design/04`：技术边界 / 确定性规则；
- `docs/design/05`：A10 / B03 / S01 等验收；
- Current Implementation 中交互、状态机、Tool 边界相关文档；
- `docs/README.md`、`docs/evidence/README.md`、Task 012 路线。

如果代码修改而 Current Implementation 文档未同步，Task 不得完成。

## 10. Evidence 07 的处理

不得删除 012C 当时的实验结论。

但要追加人工复核说明：

> 012C 实验本身不能证明生产存在缺陷；后续代码审计发现生产 Bridge 对缺失 scope 存在 WholeWellScope fallback，且现有测试把无 scope 参数修改视作成功路径。因此 `No production architecture issue found` 仅代表 012C 实验当时未建立生产因果，不代表生产语义已经与最新设计一致。该问题转入 Task 012C.1。

## 11. 完成标准

Task 只有在以下全部完成后才能关闭：

1. 四个参数的 scope 语义矩阵完成；
2. S01～S07 有真实代码级结果；
3. 明确选择 A/B/C/D 或等价正式结论；
4. 未确认的业务规则显式标记，不自行补齐；
5. 如需改代码，相关测试通过；
6. Evidence 08 完成；
7. Evidence 07 追加 post-review note；
8. 04/05 与 Current Implementation 按实际影响同步；
9. Documentation Impact 完整；
10. branch / commit / push 状态明确。

## 12. 完成后停止

完成后不要自动开始 HITL / Interrupt 或 Tracing。

先人工评审写范围最终语义，再继续 AgentScope 剩余能力验证。

---

## 13. 执行结果（2026-10-04）

### 决策与实现

选择结论 B：仅允许同一 Task / Execution 工作基线上的可信 Active scope 继承；无本轮明确范围且无匹配 Active scope 时返回 `NEED_CLARIFICATION（需要澄清）`。View scope 不赋予写权限。`por`、`perm`、`sampling_interval`、`prediction_model` 目前均仅声明整井 capability，但是否“业务上天然整井”缺少依据，逐项标记 `BUSINESS_CONFIRMATION_REQUIRED（需要业务确认）`，未补造专业规则。

实现了 Resolver 缺 scope issue、Bridge 不再把缺失写范围合成为整井、Middleware 拒绝模型候选但未被本轮文字支持的 `WHOLE_WELL`；同步调整 Mock 模型提示和旧测试夹具，使成功用例明确给出整井意图。局部 scope 仍按 capability 稳定拒绝，多 operation 的预检保持整体阻断。

### 新增 / 修改文件

- 新增 `docs/evidence/08-Task012C1-写范围语义与安全边界核对.md`。
- 修改 `src/cnlc_agent/demo/operation_context_resolver.py`、`operation_execution_bridge.py`、`interaction_middleware.py`、`demo_agent.py`、`plan_validator.py`。
- 修改范围测试：`tests/unit/test_operation_context_resolver.py`、`test_operation_execution_bridge.py`、`test_operation_clarification.py`、`test_operation_tool.py`；集成测试：`tests/integration/test_interaction_robustness.py`、`test_task_react.py`、`test_demo_web_http.py`。
- 修改 `docs/design/04-测井解释智能体技术方案.md`、`docs/design/05-测井解释智能体测试与验收方案.md`、`docs/08-intent-and-interaction-design.md`、`docs/10-interaction-state-machine.md`、`docs/evidence/README.md`、`docs/README.md`、`docs/tasks/012-agentscope-native-capability-poc.md`。
- Evidence 07 中保留 012C 原实验结论和 post-review 注记，没有改写原始数据。

### 测试与检查

- 目标测试组：`164 passed`。
- `pytest tests/unit -q`：`741 passed, 1 warning`（Starlette `BlockingPortal` 弃用提醒）。
- 相关 ReAct / Web 集成测试：`31 passed, 1 failed`。失败项 `test_mock_shell_supports_context_compression_and_keeps_task_identity` 在 AgentScope 强制压缩后把框架摘要作为最近 user-role 内容，Mock 模型从摘要中的旧井号提交引用，最终被 Bridge 以 `SESSION_WELL_NOT_FOUND` 安全拒绝；没有创建 Execution。该问题是未解决的上下文输入/引用路由回归，列为 Architecture Issue，不得视为集成测试通过。
- `pytest tests/integration -q`：`177 passed, 22 skipped, 2 failed`。`test_read_api_restores_durable_binding_before_any_new_chat` 因未设置 `DATABASE_URL` 无法启动；上下文压缩的 ReAct 用例安全拒绝旧摘要井号引用，见 Architecture Issue。PostgreSQL / Redis、模型凭证及 GDSX 文件相关 22 项按项目约定跳过。
- `ruff check src tests`：全仓失败，当前报告 222 项问题，集中在本 Task 未修改的既有 `pygdsx` / `wplm` 文件；本次触及的 12 个源代码 / 测试文件单独检查 All checks passed。`git diff --check`：通过。

### Documentation Impact

- 已更新 Evidence 08、Task 012C.1 执行结果、Task 012 路线、设计 04/05、Current Implementation 08/10、Evidence 导航和 `docs/README.md`。
- 设计 01/02/03 已检查无需改：本次没有需求、建设目标或产品效果语义变更；状态/枚举无新增，因此 `docs/11-status-enum-glossary.md` 无需修改。
- Evidence 07 保留并满足 post-review 记录要求；本次另建 Evidence 08 记录审计矩阵、决策、测试和运行问题。
- 遗留文档/实现不一致：AgentScope 压缩会话中当前输入与历史摘要的呈现/引用提取尚未解决，已在 Evidence 08 记录，不能以摘要作为 Authority。

### 架构与交付状态

- Architecture Issue：压缩后模型收到框架摘要作为最近 user-role 输入，导致模型候选 Task 引用偏向旧井；Bridge 安全拒绝但交互无法完成。需要单独定位 AgentScope Conversation Context 输入边界，当前 Task 不扩大范围修复。
- Branch：`codex/task-12-agentscope-native-capability-poc`。
- Commit：`dacec28f136c9a9b2e277c5b9ec87f86075027a7`（`feat: enforce Task 012C.1 write scope boundary`）。
- Push：已推送到远端同名分支。
- 后续依赖：四参数的自然业务粒度待业务确认；压缩会话输入问题待架构评审。完成本 Task 后停止，不自动开始 HITL / Tracing。
