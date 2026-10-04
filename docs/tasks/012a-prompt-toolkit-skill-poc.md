# Task 012A｜Prompt + Toolkit + Skill 动态工具选择 POC

> 父任务：012
> 分支：codex/task-12-agentscope-native-capability-poc
> 原则：只新增 experiments / tests / docs；不修改生产 Workflow、OperationBridge、StageOrchestrator 和持久化模型。

## 1. 目的

验证 AgentScope 2.0.8 原生 Agent + Prompt + Toolkit + Skill 是否能够稳定承担：

- 区分完整解释、查询、修改预检、比较、报告读取、阶段确认；
- 根据用户自然语言动态选择正确 Tool；
- 在需要专业方法时按需读取 Skill；
- 不支持能力能够安全拒绝，不伪造业务结果。

本任务不证明真实测井专业计算正确。

## 2. POC 目录

新增：

~~~text
experiments/agentscope_native_poc/
  README.md
  agent.py
  tools.py
  state.py
  runner.py
  skills/
    full-interpretation/SKILL.md
    query-and-compare/SKILL.md
    modify-and-rerun/SKILL.md
  cases.json
tests/experiments/
  test_native_poc_contracts.py
~~~

不要把 POC 模块 import 到生产 bootstrap。

## 3. Agent 构成

只使用一个独立 AgentScope Agent。

System Prompt 保持短：

- 你是单井常规测井解释助手；
- 正式事实必须来自 Tool；
- 不允许编造测井结果；
- 只执行 Toolkit 中已开放能力；
- 修改类请求先预检；
- 含糊对象不得直接写入。

不要把完整四阶段方法、所有查询规则和修改流程全部堆入 System Prompt；这些内容用于 Skill A/B。

## 4. Mock Tool

至少实现以下独立 Tool：

- get_current_context：返回当前 task / execution / view / scope Mock 权威上下文；
- start_full_interpretation：记录完整解释请求；
- query_interpretation_result：只读查询；
- preflight_modify_parameter：校验修改意图，返回 allowed / unsupported / need_clarification；
- apply_parameter_change：模拟写操作；默认需要权限确认的实验接口，但 012A 先不测 HITL；
- compare_result_versions：比较两个 Mock 版本；
- read_report：读取已有报告；
- confirm_stage：阶段确认。

Tool 不实现真实专业算法，不调用现有 TaskCommandRunner，不写 PostgreSQL。

每个 Tool 必须记录：call_id、tool_name、args、result、sequence。

## 5. Skill

### full-interpretation

描述完整解释四阶段的目标与可用 Tool。Skill 只能指导方法，不包含权限、数据库、版本并发和专业数值。

### query-and-compare

说明优先查询已有结果；只读请求不得启动完整解释；比较必须有两个明确版本。

### modify-and-rerun

说明修改步骤：识别目标 → 获取权威上下文 → preflight → 根据返回决定执行 / 澄清 / 拒绝 → 执行后读取新版本。

不得在 Skill 中写死 POR 对 SW 等正式专业依赖。

## 6. A/B 变量

必须至少跑两组配置：

A：短 Prompt + Toolkit，不注册 Skill
B：相同短 Prompt + 相同 Toolkit + 三个 Skill

除 Skill 外，不允许改变 Tool 描述或测试输入，避免失去对照意义。

可额外保存当前项目方案 C 的静态行为作为参考，但 012A 不修改或删除现有 Operation Tool。

## 7. 固定测试集

cases.json 至少包含：

1. 帮我解释这口井
2. 看看 2035–2038m
3. 这段怎么样
4. 只看刚才那层，不要重新算
5. 把孔隙度改成 0.16
6. 把刚才那层孔隙度改成 0.16
7. 比较修改前后
8. 看上一版报告
9. 确认并继续
10. 刚才说错了，改成 0.18
11. 帮我看看今天的天气
12. 把 Rw 改成 0.03

其中局部修改、Rw 等当前不开放能力必须返回 unsupported / clarification，不得假装执行成功。

## 8. 运行方式

必须提供两种模式：

### offline-contract

不调用 qwen-plus，只验证：
- Toolkit 能注册 Skill；
- SkillViewer 可见三个 Skill；
- Tool schema 可加载；
- POC 不 import 生产执行服务；
- Mock business state 写入和日志结构稳定。

### real-model

使用项目现有 qwen-plus 配置，逐条运行 cases.json。

真实模型模式必须显式 opt-in，例如：

~~~text
CNLC_RUN_NATIVE_POC_MODEL=1
~~~

无凭据时测试 skip，不算失败。

## 9. 证据输出

每次 real-model 运行生成 JSONL，不提交敏感内容：

~~~text
artifacts/native_poc/
  no_skill.jsonl
  with_skill.jsonl
  summary.json
~~~

每条记录包含：

- case_id
- input
- config: no_skill / with_skill
- expected_action
- actual_tool_calls
- arguments
- result
- final_response_summary
- pass
- failure_reason
- token_usage（若可取得）

禁止保存模型隐藏推理。

## 10. 通过判定

首先不设 98% 等主观准确率门槛。

必须输出逐用例对比，并重点回答：

1. 注册 Skill 后是否减少错误 Tool 选择；
2. 是否减少不必要的完整重算；
3. 是否能在修改时先调用 preflight；
4. Skill 是否引入错误专业依赖或额外 Tool；
5. Tool 描述是否已经足够，Skill 是否其实没有增益。

如果 Skill 没有明显收益，应如实记录，不为了使用 AgentScope Skill 而强行采用。

## 11. 禁止事项

- 不修改 AGENTS.md；
- 不删除现有三个项目 Agent 类；
- 不改 W01～W10；
- 不改四阶段 StageOrchestrator；
- 不改 OperationPlan / Bridge；
- 不接真实公司 API；
- 不实现层段级真实修改；
- 不把 POC Mock 结果写入正式业务表。

## 12. 验收

必须执行：

~~~text
pytest tests/experiments -q
ruff check experiments tests/experiments
git diff --check
~~~

如果本地原有测试可承受，再执行与 AgentScope / Task ReAct 相关的现有测试，确认 POC 没有污染生产 import。

完成后报告：

- 分支；
- commit；
- 新增文件；
- offline 测试结果；
- qwen-plus A/B 结果（如有凭据）；
- no_skill vs with_skill 差异；
- 初步 KEEP / REPLACE / WRAP 判断；
- 未验证项。

## 13. 推荐模型

优先：GPT-6 Astra Work，高思考等级，用于严格保持实验边界、阅读 AgentScope 2.0.8 API 并设计 A/B 证据。

额度受限时：GPT-5.6 Sol High。不要用模型能力差异替代 qwen-plus A/B 本身；实验对象仍是项目实际 qwen-plus。