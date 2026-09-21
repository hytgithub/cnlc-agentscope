# Demo Task B：Workflow + Mock Data

目标：明天演示前跑通 W01-W10 主业务链，不实现真实专业算法。

## 约束
- 保持 W01-W10 顺序。
- Demo 数据视为完整。
- W03/W04/W05/W08 使用 Mock Tool 结果。
- W06/W07 仍通过 InterpretationAgent -> ModelGateway，模型由 Task 04 统一接 qwen-plus。
- W09 在 Demo Mode 下不做真实综合验证，返回结构化“已跳过/演示模式”结果，使 W10 可继续。
- 不实现 rollback、复杂 retry、真实 QC、真实岩石物理公式。
- 不修改 ModelGateway 真实实现，不修改报告样式。

## 主要工作
1. 增加明确 Demo Mode 配置。
2. Demo Mode 下 W02 直接按“输入完整”通过。
3. 完善 mock_data/WELL_MOCK_001.json，使其至少包含：
   - well
   - raw_data
   - QC mock result
   - lithology mock result
   - petrophysics mock result
   - interval mock result
   - auxiliary evidence if schema requires
4. 确保 W03/W04/W05/W08 可从 fixture 得到 StageResult。
5. W09 Demo Mode 返回 SUCCESS 的结构化 StageResult，标记 is_mock=true / demo_skipped=true。
6. W10 能正常完成。
7. 保持非 Demo Mode 现有行为不被破坏。

## 验收
- 使用 MockModelGateway 时，现有离线测试仍通过。
- Demo Mode + 完整 fixture 能顺序走到 W10。
- W01-W10 executions 全部可查看。
- 不因缺失推荐资料进入 REVIEW_REQUIRED。
- 不新增专业阈值或公式。
- pytest / ruff / mypy 通过。

## 文件边界
优先修改：
- workflows/
- mock_data/
- demo mode 相关最小配置
- 对应 workflow tests

不要修改：
- 真实模型 adapter
- reports/assembler.py
- AgentScope Web
- PostgreSQL / Redis 架构

完成后提交并 push 到 codex/demo-workflow-mock。
