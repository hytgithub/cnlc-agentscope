# Demo Task B：Workflow + Mock Data

目标：明天演示前跑通 W01-W10 主业务链，不实现真实专业算法。

## 基线
- 集成分支：demo/2026-09-22
- 本任务分支：codex/demo-2026-09-22-workflow
- 已包含 Task 04：真实 qwen-plus ModelGateway

## 约束
- 保持 W01-W10 顺序。
- Demo 数据视为完整。
- W03/W04/W05/W08 使用 Mock Tool 结果。
- W06/W07 仍通过 InterpretationAgent -> ModelGateway，统一使用 qwen-plus。
- W09 在 Demo Mode 下不做真实综合验证，返回结构化演示结果，使 W10 可继续。
- 不实现 rollback、复杂 retry、真实 QC、真实岩石物理公式。
- 不修改 ModelGateway 真实实现，不修改报告样式。

## 主要工作
1. 增加明确 Demo Mode 配置。
2. Demo Mode 下 W02 按“输入完整”通过。
3. 完善 mock_data/WELL_MOCK_001.json：
   - well
   - raw_data
   - QC mock result
   - lithology mock result
   - petrophysics mock result
   - sw mock result
   - interval mock result
   - schema 所需 auxiliary evidence
4. 确保 W03/W04/W05/W08 从 fixture 得到 StageResult。
5. W09 Demo Mode 返回 SUCCESS 的结构化 StageResult，标记 demo_skipped=true / is_mock=true。
6. W10 正常完成。
7. 非 Demo Mode 现有行为不受影响。

## 验收
- Demo Mode + 完整 fixture 顺序走到 W10。
- W01-W10 executions 全部可查看。
- 不因缺失资料阻塞 Demo。
- 不新增专业阈值或公式。
- pytest / ruff / mypy 通过。

## 文件边界
优先修改：
- workflows/
- mock_data/
- demo mode 相关最小配置
- workflow tests

不要修改：
- infrastructure/model_gateway.py
- reports/assembler.py
- PostgreSQL / Redis 架构
- Web

完成后提交并 push 到 codex/demo-2026-09-22-workflow。
