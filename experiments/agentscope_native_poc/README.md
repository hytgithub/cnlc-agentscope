# Task 012A：AgentScope Prompt + Toolkit + Skill 对照实验

## Task 012B：多轮 Context 与 Authority

独立运行多轮会话实验，不调用生产 Resolver、TaskCommandRunner、Workflow、PostgreSQL 或 Redis。AgentState 只保存 AgentScope 会话消息；task / well / execution / scope / version / revision 由 `authority.py` 的隔离 Fixture 确定。

离线合同测试：

```bash
pytest tests/experiments/test_multiturn_context_authority.py -q
```

真实 qwen-plus A/B（AgentState 连续上下文 vs 每轮新 Agent）需显式启用。`CNLC_MODEL_ENV_FILE` 可指向本机已有模型配置文件；日志和摘要不会记录密钥：

```bash
CNLC_RUN_012B_MODEL=1 CNLC_MODEL_ENV_FILE=/path/to/project/.env \
  python -m experiments.agentscope_native_poc.multiturn_runner --real-model --repeat 3
```

逐轮证据保存在 `experiments/agentscope_native_poc/artifacts/task012b_<experiment-id>.jsonl`，汇总写入同目录的 `task012b_summary.json`。汇总分别列出 Reference Understanding、Authority Binding、Write Safety、Tool Route、Grounding，并独立列出任何 Write Safety 失败。固定集为 M01–M07，定义见 `multiturn_cases.json`。

若 Grounding 规则调整，可使用 `--reevaluate-experiment-id <experiment-id>` 仅基于 JSONL 离线重算，不会重复调用模型。

Task 012B.1 仅补跑 M01–M06 的第 4、5 次，不重跑已满足最低次数的 M07；沿用同一 Prompt、Toolkit、Fixture 和 evaluator，不调整模型行为。真实 qwen-plus 增量运行命令：

```bash
CNLC_RUN_012B_MODEL=1 CNLC_MODEL_ENV_FILE=/path/to/project/.env \
  python -m experiments.agentscope_native_poc.multiturn_runner --real-model --task012b1-topup
```

增量原始记录写入 `artifacts/task012b1_<experiment-id>.jsonl`，摘要写入 `artifacts/task012b1_summary.json`；运行器拒绝覆盖同名 Task 012B.1 证据。每条记录用 `repeat_index` 3/4 表示第 4/5 次。

该目录是独立的 AgentScope 2.0.8 POC。`no_skill` 与 `with_skill` 共用基础 System Prompt、八个 Mock Tool、场景输入和 Fixture；实验变量是 Toolkit 是否加载本地 Skill。Skill 元信息由 AgentScope 自动追加到有效 system prompt，这是 Skill 配置本身的预期差异。所有 Tool 结果都标注为 Task 012A 测试 Fixture，不代表真实测井数据，也不调用专业算法、数据库、Redis 或公司 API。

Tool 的状态值仅属于本实验：`ALLOWED`（允许模拟修改）、`UNSUPPORTED`（当前 Fixture 不支持）、`NEED_CLARIFICATION`（需要补充信息）、`SIMULATED`（仅模拟 Fixture 变化）、`OK`（只读调用成功）、`REJECTED`（Mock 拒绝）和 `NOT_FOUND`（Fixture 中无此对象）。Tool 只访问进程内状态，不发起 I/O，因此没有外部 Tool 超时配置。

离线契约测试：

```bash
pytest tests/experiments -q
ruff check experiments tests/experiments
```

如需逐场景调用项目现有 qwen-plus 配置并生成 A/B 证据，先设置 `CNLC_RUN_NATIVE_POC_MODEL=1`，再运行：

```bash
uv run python -m experiments.agentscope_native_poc.runner
```

没有完整 `qwen-plus` 凭据时运行器返回 `SKIP`，不会发出模型请求。

## Task 012A.1：Skill 读取、重复运行与 Grounding

Task 012A.1 在此隔离目录内增加四个 Skill-required 场景，并默认对 case `02`、`03`、`05`、`06`、`07`、`09`、`10`、`13`、`14`、`15`、`16` 重复运行五次；其余固定场景运行一次。重复次数可通过 CLI 或环境变量调整：

```bash
CNLC_RUN_NATIVE_POC_MODEL=1 \
CNLC_NATIVE_POC_REPEAT=5 \
CNLC_NATIVE_POC_MODEL_TIMEOUT_SECONDS=120 \
uv run python -m experiments.agentscope_native_poc.runner --repeat 5
```

也可以用 `--repeat-case-ids 02,03,14,15` 改变重复场景。no_skill 和 with_skill 对每个场景使用相同输入、基础 Prompt、业务 Tool Schema 和初始 Fixture；二者按重复序号交替先后次序。运行器不把“Skill 被注册”当作“模型读过 Skill”：逐条记录元信息可见性、模型发出的 `Skill` ToolCall、所选 Skill、步骤序号，以及 Skill 正文是否作为 ToolResult 返回给模型。

单条模型请求默认 120 秒超时，可用 `--model-timeout-seconds` 或 `CNLC_NATIVE_POC_MODEL_TIMEOUT_SECONDS` 调整。进程中断后可以用 `--resume-experiment-id <experiment_id>` 从对应 JSONL 续跑缺失的 case/run 配对；运行完成后可用 `--reevaluate-task012a1` 只重算评分，不再调用模型。

每次运行生成独立 JSONL：

```text
artifacts/native_poc/task012a1_<experiment_id>_no_skill.jsonl
artifacts/native_poc/task012a1_<experiment_id>_with_skill.jsonl
```

`summary.json` 保存聚合路由率、SkillViewer 调用率、参数正确率、非法写入尝试数、不必要完整解释数、澄清正确率、Grounding 通过率和 overall 通过率，并保留 Task 012A 前次摘要。最小 Grounding evaluator 只检查本 POC 可确定的边界，不是通用幻觉检测器；Tool 调用正确但 Grounding 失败时，overall 仍为失败。

## Task 012C：ReAct 与 AgentScope 原生 Task Tools

Task 012C 在同一 Prompt、业务 Toolkit 和 authority Fixture 下比较普通 ReAct 与额外注册 AgentScope 2.0.8 `TaskCreate` / `TaskGet` / `TaskList` / `TaskUpdate` 的配置；不在 Prompt 中要求使用 Task 工具。该会话任务只属于 AgentState，不作为业务 Task、Execution、范围或版本事实。运行 qwen-plus 组时，当前工作目录须能读取项目已有模型配置：

```bash
python -m experiments.agentscope_native_poc.plan_runner --repeats 5 --timeout 120
```

可先离线核验当前实际安装环境中的 import、Toolkit 注册、AgentState 注入与 Task CRUD：

```bash
python -m experiments.agentscope_native_poc.plan_runner --smoke-only
```

正式运行会生成 `artifacts/task012c_<experiment_id>_plain.jsonl`、`_plan.jsonl` 与 `task012c_summary.json`。评分规则修订后，使用原始 ToolCall / ToolResult 离线复核，不重复调用模型：

```bash
python -m experiments.agentscope_native_poc.plan_runner \
  --reevaluate-experiment-id <experiment_id>
```

复核结果单独写入 `task012c_<experiment_id>_rescored_{plain,plan}.jsonl`，并保留初始评分供审计。固定输入在 `plan_cases.json`，合同测试在 `tests/experiments/test_task_plan_poc.py`；实验边界与结论见 `docs/evidence/07-Task012C-TaskPlan多步骤对照实验结果.md`。

## Task 012D：HITL / Interrupt 隔离 A/B

`hitl_runner.py` 使用 AgentScope 2.0.8 原生 Agent / Permission / Event，包装现有 Mock TaskCommandRunner / Operation / StageOrchestrator。012D 明确要求复用现有业务确认能力，故这一模块允许导入生产应用入口；012A～012C 的独立 Mock 隔离约束继续保留。没有新业务状态机、真实专业规则或生产 UI 迁移。

```bash
.venv/bin/pytest tests/experiments/test_hitl_interrupt_poc.py -q
```

28 个测试覆盖 H01～H09；离线脚本模型只替代公网模型响应，权限暂停、Tool、业务状态与确认运行实际实现。若要记录原生事件，给 `CNLC_HITL_EVIDENCE` 指定新 JSONL 路径；测试结束后追加各 case 的实际发出事件，需结合 pytest/JUnit 结果判定成功，单有 JSONL 不表示测试通过。

结果见 `artifacts/native_poc/task012d_hitl_{events.jsonl,tests.xml,summary.json}` 与 [Evidence 10](../../docs/evidence/10-Task012D-HITL-Interrupt对照实验结果.md)。本轮判定业务确认 `KEEP（保留）`，生产阶段接入 `NO_CLEAR_VALUE（没有明确收益）`，局部交互 `WRAP（包装）` 候选；序列化恢复不能泛化为实际浏览器/HTTP/持久服务验收。

Tool Contract：`confirm_checkpoint` 仅确认固定 Fixture 目标；输入为 task_id/execution_id/stage/stage_run_id，输出为 fixture=true、同一目标与 execution_status；非法事件抛 ValueError，业务旧目标/无权限使用现有 InfrastructureError（基础设施/业务归属错误）；调用复用 Runner 的 timeout/lease 配置，不增加重试；原生状态为 asking/allowed/denied/interrupted（等待确认/允许执行/被拒绝/已中断），业务状态仍使用现有 StageRun；Mock 实现复用现有专业工具，测试见上述文件。`external_checkpoint` 仅产生原生外部等待事件，H08 用独立 Mock 作业验证中断和迟到结果隔离，不访问真实 API 或提供取消能力。
