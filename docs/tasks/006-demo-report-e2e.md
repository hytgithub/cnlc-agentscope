# Demo Task C：Report + E2E Demo

目标：把已经完成的 InterpretationState 以适合明天演示的形式输出为 JSON + Markdown，并提供单命令 E2E 演示。

## 约束
- 不实现真实专业算法。
- 不修改 ModelGateway。
- 不修改 W01-W10 的业务编排规则。
- 不增加新 Agent。
- 报告必须明确 Demo/Mock 来源，不能把 Mock 结果表述成真实专业结论。

## 主要工作
1. 优化 ReportAssembler：
   - 标题改为单井测井解释演示报告；
   - 按 W03-W10 业务结果分节；
   - 能展示岩性、物性、流体、层分类、层段结果；
   - 输出 JSON 和 Markdown；
   - 对 is_mock/demo_skipped 做清晰标记。
2. 增加一条最简单的演示入口：
   - 优先复用现有 CLI；
   - 输入 WELL_MOCK_001；
   - 一次执行完成 workflow；
   - 在 outputs/ 生成 JSON + Markdown。
3. 增加 E2E 测试：
   - workflow 成功；
   - JSON 文件存在且可解析；
   - Markdown 文件存在；
   - 报告包含 W06/W07 结果；
   - 报告不泄漏模型凭据或原始异常。
4. README 增加“Demo Run”最小运行说明。

## 验收
- 一条命令可跑完整演示。
- 最终状态 SUCCESS 或允许的 WARNING。
- 输出 JSON + Markdown。
- pytest / ruff / mypy / build 通过。

## 文件边界
优先修改：
- reports/
- CLI / application entrypoint
- tests/integration/
- README
- outputs 生成逻辑

不要修改：
- ModelGateway 实现
- 专业 Tool 算法
- PostgreSQL / Redis 架构
- Workflow 核心规则（如发现依赖问题，记录并交给 Integration Owner）

完成后提交并 push 到 codex/demo-report-e2e。
