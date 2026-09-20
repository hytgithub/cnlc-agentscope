import json

from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.state import InterpretationState


class ReportAssembler:
    def to_json(self, state: InterpretationState) -> str:
        return state.model_dump_json(indent=2)

    def to_markdown(self, state: InterpretationState) -> str:
        complete = state.status in {StepStatus.SUCCESS, StepStatus.WARNING}
        lines = [
            "# 单井测井解释骨架演示报告" if complete else "# 单井解释任务诊断摘要",
            "",
            "> Mock 演示：专业参数和结论为测试预设，不代表真实测井解释。",
            "",
            f"- 任务：{state.task.task_id}",
            f"- 井：{state.task.well_id}",
            f"- 状态：{state.status.value}",
            f"- 已完成步骤：{', '.join(state.completed_steps) or '无'}",
            "",
        ]
        if state.well:
            lines.extend(["## 井基本信息", "", f"井名：{state.well.name}", ""])
        if state.raw_data:
            lines.extend(
                [
                    "## 数据情况",
                    "",
                    f"深度采样点：{len(state.raw_data.depths)}；"
                    f"曲线：{', '.join(state.raw_data.curves)}；"
                    f"深度单位：{state.raw_data.depth_unit}（{state.raw_data.depth_reference}）。",
                    "",
                ]
            )
        sections = [
            ("质量控制", state.qc_result),
            ("岩性识别", state.lithology_result),
            ("储层与物性", state.petrophysics_result),
            ("流体识别", state.fluid_result),
            ("油气水层分类", state.layer_classification),
            ("层段与厚度", state.interval_result),
            ("综合验证", state.validation_result),
            ("最终检查", state.final_check),
        ]
        for title, result in sections:
            if result is None:
                continue
            lines.extend(
                [
                    f"## {title}",
                    "",
                    "```json",
                    json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
                    "```",
                    "",
                ]
            )
        lines.extend(["## 缺失资料与提示", ""])
        lines.extend(
            f"- {item.importance}: {item.field}（影响 {item.affected_step}）"
            for item in state.missing_data
        )
        lines.extend(f"- {warning}" for warning in state.warnings)
        lines.extend(f"- {error.code}: {error.message}" for error in state.errors)
        if not (state.missing_data or state.warnings or state.errors):
            lines.append("当前演示数据未触发缺失或执行错误。")
        lines.extend(["", "## 执行结论", ""])
        lines.append(
            "Mock 骨架链路已完成；真实模型、专业算法与基础设施尚待接入。"
            if complete
            else "任务未完成，以上结果仅供定位问题；不能形成最终专业解释结论。"
        )
        return "\n".join(lines) + "\n"
