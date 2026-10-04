# Task 012A｜Prompt + Toolkit + Skill 对照实验结果

> 日期：2026-10-04  
> 分支：`codex/task-12-agentscope-native-capability-poc`  
> 实现提交：`ffa2539bac712d0527f00ecf00c9ea3636da03ab`  
> 模型：qwen-plus  
> 结论状态：**PARTIAL（部分验证）**

## 1. 实验目的

比较两组条件：

- A：短 Prompt + 相同 Toolkit + 不注册 Skill；
- B：相同短 Prompt + 相同 Toolkit + 注册三个 AgentScope Skill。

除 Skill 注册外，业务 Mock Tool、测试 Fixture 和 12 条场景保持一致。

## 2. 已验证事实

| 项目 | 结果 |
|---|---|
| AgentScope Toolkit 可注册多个独立业务 Tool | 已验证 |
| AgentScope 2.0.8 可加载本地 Skill | 已验证 |
| SkillViewer 可在离线测试中读取三个 SKILL.md | 已验证 |
| qwen-plus 能动态选择部分业务 Tool | 已验证 |
| no_skill | 7 / 12 |
| with_skill | 9 / 12 |
| qwen-plus 实际 SkillViewer 调用 | **0 次** |

原始证据：

- `artifacts/native_poc/no_skill.jsonl`
- `artifacts/native_poc/with_skill.jsonl`
- `artifacts/native_poc/summary.json`
- `tests/experiments/test_native_poc_contracts.py`

## 3. 有改善的场景

with_skill 相比 no_skill 在本轮单次运行中改善：

- “看看 2035–2038m”：从只查上下文改善为继续调用 `query_interpretation_result`；
- “比较修改前后”：从未调用业务 Tool 改善为 `get_current_context → compare_result_versions`；
- “刚才说错了，改成 0.18”：从错误 UNSUPPORTED 改善为 NEED_CLARIFICATION。

## 4. 退化和共同失败

退化：

- “把刚才那层孔隙度改成 0.16”：no_skill 能执行 `context → preflight` 并得到 UNSUPPORTED；with_skill 只读取 context 后停止，未调用 preflight。

两组共同失败：

- “帮我解释这口井”；
- “这段怎么样”。

## 5. 关键证据边界

### 5.1 不能证明完整 Skill 指令产生了提升

虽然 with_skill 为 9/12，但 qwen-plus 在 12 个场景中：

```text
SkillViewer calls = 0
```

因此当前最多能说明：

> 注册 Skill 后暴露出的 Skill 元信息可能影响了模型决策。

不能说明：

> 模型读取并执行了完整 SKILL.md，因而表现提升。

### 5.2 当前得分主要是 Tool 路由得分

判分主要依据：

- Tool 名；
- Tool 参数；
- 顺序；
- Tool 返回状态。

最终自然语言回复仍存在未经 Tool 支撑的参数举例等问题，因此：

```text
9 / 12 != 整体 Agent 正确率
```

后续测试需要增加 Response Grounding：最终回复中的业务事实必须可追溯到 ToolResult 或权威业务状态。

### 5.3 单次样本不足以证明稳定提升

当前每个场景每种配置只执行一次，不能据此判断稳定性。

## 6. 当前架构决策

| 能力 | 当前结论 |
|---|---|
| 多个清晰业务 Tool + Agent 动态选择 | **PARTIAL/POSITIVE：已有直接运行证据** |
| Skill 注册与加载 | **PASS** |
| qwen-plus 主动读取完整 Skill | **FAIL TO PROVE：本轮 0 次** |
| Skill 稳定改善 Tool 选择 | **VERIFY：有信号但证据不足** |
| Skill 替代 Operation 层 | **不得下结论** |
| 删除现有 Operation / Resolver / Validator | **禁止** |

当前最合理的架构判断是：

> AgentScope Agent / Toolkit 可以承担一部分自然语言到业务 Tool 的动态选择；现有确定性权限、版本、引用、参数和执行安全层继续保留。Skill 是否值得承担“方法层”职责，需要 012A.1 进一步验证。

## 7. 对主设计文档的影响

### 对 01～03

无直接修改：
- 需求不变；
- 建设目标不变；
- 预期效果不变。

### 对 04 技术方案

当前只能把以下判断升级为“已有初步证据”：

> AgentScope Agent + Toolkit 可以直接动态选择多个受控业务 Tool。

Skill 仍应保持“验证中”。

### 对 05 测试与验收

后续应增加两类验收：

1. 重复运行稳定性；
2. Response Grounding：最终回复中的业务事实必须来自 ToolResult / Domain State，不得因 Tool 路由正确就判定整个 Agent 通过。

## 8. 下一步建议

先做 012A.1，而不是直接删除 Operation 代码：

- 设计能真正触发 SkillViewer 的场景；
- 同一 case 重复运行，获得稳定性数据；
- 增加最终回复 Grounding 校验；
- 再决定 Skill 是 KEEP / WRAP / 暂无明显价值。