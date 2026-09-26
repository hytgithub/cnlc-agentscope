"""把既有状态渲染为结果文件，不新增地质解释或修改 Workflow 决策。"""

import json
import re
from typing import Any

from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.reports.formatter import MISSING, is_missing
from cnlc_agent.reports.generator import ReportGenerator
from cnlc_agent.reports.models import ReportStyle

LABELS = {
    "quality": "QC 质量",
    "correction_applied": "是否校正",
    "lithology": "岩性",
    "vsh": "泥质含量",
    "porosity": "孔隙度",
    "permeability": "渗透率",
    "reservoir": "储层",
    "water_saturation": "含水饱和度",
    "fluid_type": "流体类型",
    "layer_type": "层类型",
    "intervals": "层段",
    "top_depth_m": "顶深（m）",
    "bottom_depth_m": "底深（m）",
    "gross_thickness_m": "总厚度（m）",
    "effective_thickness_m": "有效厚度（m）",
    "summary": "摘要",
}


def _safe(value: Any) -> Any:
    """在导出副本中脱敏凭据字段和原始错误消息，不修改持久化状态。"""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if re.search(r"api.?key|password|secret|token|authorization|raw.?exception", key, re.I):
                result[key] = "[REDACTED]"
            elif key == "message" and "code" in value:
                result[key] = "执行异常；请根据错误代码排查。"
            else:
                result[key] = _safe(item)
        return result
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"\bsk-[\w.\-]+|(?i:Bearer)\s+\S+", "[REDACTED]", value)
    return value


def _cell(value: Any) -> str:
    if is_missing(value):
        return MISSING
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
        .replace("\n", " / ")
        .replace("\r", "")
    )


def _rows(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        if "value" in value and set(value) <= {"value", "unit"}:
            return [f"| {_cell(prefix)} | {_cell(value['value'])} {_cell(value.get('unit', ''))} |"]
        rows = []
        for key, item in value.items():
            label = LABELS.get(str(key), str(key))
            rows.extend(_rows(item, f"{prefix} / {label}" if prefix else label))
        return rows
    if isinstance(value, list):
        return [row for i, item in enumerate(value, 1) for row in _rows(item, f"{prefix} {i}")]
    return [f"| {_cell(prefix)} | {_cell(value)} |"]


class ReportAssembler:
    """根据任务终态选择正式报告或失败诊断报告。"""

    def __init__(self, style: ReportStyle = ReportStyle.STANDARD) -> None:
        self.generator = ReportGenerator(style)

    def to_json(self, state: InterpretationState) -> str:
        """导出脱敏 JSON，同时保持 InterpretationState 原有 Schema。"""

        # 只处理 model_dump 产生的副本，绝不原地修改持久化状态。
        return json.dumps(_safe(state.model_dump(mode="json")), ensure_ascii=False, indent=2)

    def to_markdown(self, state: InterpretationState) -> str:
        """成功/告警任务生成解释报告，其余终态生成定位问题用诊断摘要。"""

        complete = state.status in {StepStatus.SUCCESS, StepStatus.WARNING}
        if complete:
            return self.generator.generate(state)
        # 诊断报告同样基于脱敏后的数据，避免错误详情泄漏敏感配置。
        data = json.loads(self.to_json(state))
        lines = [
            "# 单井解释任务诊断摘要",
            "",
            "> Demo / Mock 演示：Mock 专业参数和结论为测试预设，不代表真实测井解释。",
            "> 非 Mock 输出也可能基于 Mock 输入；不代表经过专业验证的真实结论。",
            "",
            "## 井基本信息",
            "",
            f"- 任务：{_cell(data['task']['task_id'])}",
            f"- 井号：{_cell(data['task']['well_id'])}",
            f"- 井名：{_cell(data['well']['name']) if data['well'] else '未加载'}",
            f"- 状态：{state.status.value}",
            "",
            "## 数据概况",
            "",
        ]
        raw = data["raw_data"]
        if raw:
            lines.append(
                f"深度采样点：{len(raw['depths'])}；深度单位：{raw['depth_unit']}"
                f"（{raw['depth_reference']}）。"
            )
            if raw["depths"]:
                lines.append(f"深度范围：{raw['depths'][0]}–{raw['depths'][-1]} m。")
            lines.extend(["", "| 曲线 | 单位 | 有效采样数 |", "|---|---|---|"])
            for name, curve in raw["curves"].items():
                count = sum(v is not None for v in curve["values"])
                lines.append(f"| {_cell(name)} | {_cell(curve['unit'])} | {count} |")
        else:
            lines.append("数据未加载。")
        lines.extend(
            [
                "",
                "## 步骤执行与 Demo Skip",
                "",
                "Demo Skip 仅依据显式 demo_skipped 标记；SKIPPED 表示执行记录跳过。",
                "缺失结果不视为 Demo Skip。",
                "",
                "| 步骤 | 执行状态 |",
                "|---|---|",
            ]
        )
        for execution in data["executions"]:
            lines.append(f"| {execution['step_id']} | {execution['status']} |")
        sections = [
            ("QC 质量控制", "qc_result"),
            ("岩性识别", "lithology_result"),
            ("储层与物性", "petrophysics_result"),
            ("流体识别", "fluid_result"),
            ("油气水层分类", "layer_classification"),
            ("层段划分与厚度", "interval_result"),
            ("综合验证状态", "validation_result"),
            ("最终检查", "final_check"),
        ]
        for title, field in sections:
            lines.extend(["", f"## {title}", ""])
            result = data[field]
            if result is None:
                lines.append("未生成结果；不推断专业结论。")
                continue
            skipped = (
                result.get("demo_skipped") is True or result["result"].get("demo_skipped") is True
            )
            lines.extend(
                [
                    f"- 状态：{result['status']}",
                    f"- 来源：{_cell(result['source'])}；is_mock={str(result['is_mock']).lower()}"
                    f"（{'Mock 预设' if result['is_mock'] else '非 Mock 输出'}）",
                    f"- Demo Skip：{'是；本步骤未做真实业务验证' if skipped else '无显式标记'}",
                ]
            )
            if "validation_status" in result:
                lines.append(
                    f"- 综合验证：{result['validation_status']}"
                    + ("（Demo Skip，不代表真实验证通过）" if skipped else "")
                )
            lines.extend(["", "| 项目 | State 中的结果 |", "|---|---|"])
            lines.extend(_rows(result["result"]) or ["| 结果 | 未提供 |"])
            lines.append("")
            for key, label in [
                ("evidence", "证据"),
                ("conflicts", "冲突"),
                ("missing_evidence", "缺失证据"),
                ("warnings", "提示"),
            ]:
                lines.extend(f"- {label}：{_cell(item)}" for item in result[key])
            lines.extend(["", f"建议动作：{_cell(result['recommended_action'])}"])
        lines.extend(["", "## 缺失资料与提示", ""])
        lines.extend(
            f"- {item['importance']}: {_cell(item['field'])}（影响 {item['affected_step']}）"
            for item in data["missing_data"]
        )
        lines.extend(f"- {_cell(item)}" for item in data["warnings"])
        lines.extend(f"- 错误代码：{_cell(item['code'])}（执行异常）" for item in data["errors"])
        if not (data["missing_data"] or data["warnings"] or data["errors"]):
            lines.append("当前演示数据未触发缺失或执行错误。")
        lines.extend(
            [
                "",
                "## 最终执行结果",
                "",
                f"最终状态：{state.status.value}；需要人工复核：{state.review_required}。",
                "任务未完成，以上结果仅供定位问题；不能形成最终专业解释结论。",
            ]
        )
        return "\n".join(lines) + "\n"
