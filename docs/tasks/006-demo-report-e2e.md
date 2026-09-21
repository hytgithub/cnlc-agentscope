# Demo Task C：Report + E2E Demo

目标：把完成的 InterpretationState 输出为适合演示的 JSON + Markdown，并提供单命令 E2E 演示。

## 基线
- 集成分支：demo/2026-09-22
- 本任务分支：codex/demo-2026-09-22-report
- 已包含 Task 04：真实 qwen-plus ModelGateway

## 约束
- 不实现真实专业算法。
- 不修改 ModelGateway。
- 不修改 W01-W10 核心编排规则。
- 不增加新 Agent。
- 报告必须明确 Demo/Mock 来源，不能把 Mock 结果表述成真实专业结论。

## 主要工作
1. 优化 ReportAssembler：
   - 标题：单井测井解释演示报告；
   - 展示岩性、物性、流体、层分类、层段结果；
   - 输出 JSON 和 Markdown；
   - 清晰标记 is_mock / demo_skipped。
2. 增加最简单演示入口：
   - 优先复用现有 CLI；
   - 输入 WELL_MOCK_001；
   - 一次执行完整 workflow；
   - outputs/ 生成 JSON + Markdown。
3. E2E 测试：
   - workflow 成功；
   - JSON 可解析；
   - Markdown 存在；
   - 报告包含 W06/W07；
   - 不泄漏鉴权凭据或原始异常。
4. README 增加 Demo Run。

## 验收
- 一条命令可跑完整演示。
- 最终状态 SUCCESS 或允许的 WARNING。
- JSON + Markdown 输出存在。
- pytest / ruff / mypy / build 通过。

## 文件边界
优先修改：
- reports/
- CLI / application entrypoint
- tests/integration/
- README
- outputs 生成逻辑

不要修改：
- infrastructure/model_gateway.py
- 专业 Tool 算法
- PostgreSQL / Redis 架构
- Workflow 核心规则

完成后提交并 push 到 codex/demo-2026-09-22-report。

## 执行记录（2026-09-21）

- 在既有 `codex/demo-2026-09-22-report` 分支实现，不修改 main。
- ReportAssembler 输出可读 Markdown，保留来源、Mock、显式 Demo Skip、证据、
  冲突、缺失资料及最终状态；只渲染已有 State，不新增业务判断。
- CLI 保留 `outputs/<task_id>/result.json`、`report.md` 规范；JSON 经过报告层导出，
  隐藏凭据字段及原始 ErrorDetail 消息，保留稳定错误代码，原始 State 不被修改。
- 新增五个离线测试场景：完整 CLI SUCCESS、WARNING、导出脱敏与 State 不变、
  显式 Demo Skip、CLI 模型鉴权失败（本地 HTTP 服务，不调用公网模型）。
- 实际运行 WELL_MOCK_001：SUCCESS，W01–W10 完成，两份文件生成。
- 验收：pytest 68 passed / 4 skipped；ruff check、format check、mypy、uv build 通过。
  四个跳过项为需显式启用的真实模型和真实 PostgreSQL/Redis 测试。

### Integration Dependency

Task 005 未合入。当前通过的是 Mock Workflow E2E，不是 qwen-plus 最终 Demo E2E。
待 Task 005 合入后，在集成分支核对 Demo Skip 字段（当前支持阶段结果内
`result.demo_skipped=true` 和执行记录 `SKIPPED`），再通过相同 CLI 运行真实模型，
确认 W06/W07 结果、W09 Demo Skip、W10 完成及 JSON/Markdown 输出。
未为绕过依赖修改 Workflow、ModelGateway 或专业算法。

### Architecture Issue

No Architecture Issue found.
