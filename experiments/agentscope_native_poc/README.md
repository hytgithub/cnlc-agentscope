# Task 012A：AgentScope Prompt + Toolkit + Skill 对照实验

该目录是独立的 AgentScope 2.0.8 POC。`no_skill` 与 `with_skill` 共用 System Prompt、八个 Mock Tool、场景输入和 Fixture；唯一实验变量是 Toolkit 是否加载三个本地 Skill。所有 Tool 结果都标注为 Task 012A 测试 Fixture，不代表真实测井数据，也不调用专业算法、数据库、Redis 或公司 API。

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

没有完整 `qwen-plus` 凭据时运行器返回 `SKIP`，不会发出模型请求。运行结果写入 `artifacts/native_poc/no_skill.jsonl`、`with_skill.jsonl` 和 `summary.json`；记录 Tool 调用、参数、Mock 结果及最终答复摘要，不保存模型隐藏推理。
