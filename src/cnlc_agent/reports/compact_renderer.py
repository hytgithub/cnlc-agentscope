"""面向业务阅读的精简版测井解释报告。"""

from collections import Counter

from cnlc_agent.reports.formatter import MISSING, markdown_cell, measurement, number
from cnlc_agent.reports.models import InterpretedLayer, NormalizedInterpretationResult


class CompactReportRenderer:
    """用八个固定章节呈现核心事实，省略标准版的封面和细化说明。"""

    def render(self, result: NormalizedInterpretationResult) -> str:
        """把只读归一化结果渲染为 Markdown，不回写任何业务状态。"""

        well = result.well
        title = well.name or well.well_id
        lines = [f"# {markdown_cell(title)}测井解释报告", ""]
        if result.is_mock:
            lines.extend(
                [
                    "> Demo / Mock 演示报告：仅整理结构化演示结果，不代表真实专业解释结论。",
                    "",
                ]
            )
        lines.extend(
            [
                "## 一、井基本概况",
                "",
                f"{markdown_cell(title)}本次测井解释井段为{markdown_cell(well.logging_interval)}，"
                f"解释目的为{markdown_cell(well.drilling_purpose)}。",
                "",
                "| 项目 | 内容 |",
                "|---|---|",
                f"| 井名 | {markdown_cell(well.name)} |",
                f"| 井号 | {markdown_cell(well.well_id)} |",
                f"| 井别 | {markdown_cell(well.well_category)} |",
                f"| 井型 | {markdown_cell(well.well_type)} |",
                f"| 完钻井深 | {_depth(well.total_depth_m)} |",
                f"| 完钻层位 | {markdown_cell(well.completion_formation)} |",
                f"| 构造位置 | {markdown_cell(well.structural_location)} |",
                f"| 测井井段 | {markdown_cell(well.logging_interval)} |",
                f"| 钻井液情况 | {markdown_cell(well.drilling_fluid)} |",
                f"| 本次解释目的 | {markdown_cell(well.drilling_purpose)} |",
                "",
                "## 二、资料概况与质量评价",
                "",
                "### 2.1 输入资料",
                "",
            ]
        )
        lines.append(
            f"常规测井资料包括：{'、'.join(map(markdown_cell, result.curve_names))}。"
            if result.curve_names
            else "本次未提供可识别的常规测井曲线。"
        )
        lines.extend(
            [
                f"- 录井资料：{markdown_cell(result.mud_logging_summary)}",
                f"- 岩心资料：{markdown_cell(result.core_summary)}",
                f"- 试油/试气资料：{markdown_cell(result.well_test_summary)}",
                f"- 邻井资料：{markdown_cell(result.offset_well_summary)}",
                "",
                "### 2.2 测井资料质量",
                "",
            ]
        )
        lines.extend(
            markdown_cell(item)
            for item in (result.quality_summary or ("本次未形成结构化质量评价。",))
        )
        lines.extend(
            [
                "",
                "## 三、储层测井解释",
                "",
                "### 3.1 岩性解释",
                "",
                _summary_sentence("现有解释层段岩性为", result.lithology_summary),
                "",
                "### 3.2 储层识别与物性评价",
                "",
                _summary_sentence("现有储层评价包括", result.reservoir_summary),
                "",
                "### 3.3 流体性质解释",
                "",
                _summary_sentence("现有流体及含流体层解释包括", result.fluid_summary),
                "",
                "### 3.4 油气水层解释",
                "",
                _classification_sentence(result.layers),
                "",
                "## 四、主要解释成果",
                "",
                "| 层号 | 层位 | 顶深(m) | 底深(m) | 厚度(m) | 有效厚度(m) "
                "| 孔隙度 | 渗透率 | 电阻率 | 含油饱和度 | 解释结论 |",
                "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        if result.layers:
            lines.extend(_result_row(layer) for layer in result.layers)
        else:
            lines.append("| " + " | ".join([MISSING] * 11) + " |")
        lines.extend(["", "## 五、重点层段评价", ""])
        if result.layers:
            for layer in result.layers:
                lines.extend(_layer_section(layer))
        else:
            lines.append("本次未形成可供逐层评价的结构化解释层段。")
        lines.extend(["", "## 六、综合评价", "", _overall(result), ""])
        lines.extend(["## 七、试油及进一步评价建议", ""])
        if result.recommendations:
            lines.extend(
                f"{index}. {markdown_cell(value)}"
                for index, value in enumerate(result.recommendations, 1)
            )
        else:
            lines.append("现有结构化结果未提供试油或进一步评价建议，不据此新增结论。")
        lines.extend(["", "## 八、存在问题及解释局限", ""])
        if result.limitations:
            lines.extend(f"- {markdown_cell(value)}" for value in result.limitations)
        else:
            lines.append("现有资料基本满足本次解释需要，未发现明显影响主要解释结论的问题。")
        return "\n".join(lines).rstrip() + "\n"


def _result_row(layer: InterpretedLayer) -> str:
    return (
        f"| {markdown_cell(layer.layer_id)} | {markdown_cell(layer.formation)} "
        f"| {number(layer.top_depth_m)} | {number(layer.bottom_depth_m)} "
        f"| {number(layer.gross_thickness_m)} | {number(layer.effective_thickness_m)} "
        f"| {measurement(layer.porosity)} | {measurement(layer.permeability)} "
        f"| {measurement(layer.resistivity)} | {measurement(layer.hydrocarbon_saturation)} "
        f"| {markdown_cell(layer.layer_type)} |"
    )


def _layer_section(layer: InterpretedLayer) -> list[str]:
    name = " ".join(value for value in (layer.formation, layer.layer_id) if value) or "解释层段"
    parameters = [
        f"孔隙度{measurement(layer.porosity)}" if measurement(layer.porosity) != MISSING else None,
        f"渗透率{measurement(layer.permeability)}"
        if measurement(layer.permeability) != MISSING
        else None,
        f"电阻率{measurement(layer.resistivity)}"
        if measurement(layer.resistivity) != MISSING
        else None,
    ]
    traits = "，".join(value for value in parameters if value) or MISSING
    evidence = "；".join(map(markdown_cell, layer.evidence)) if layer.evidence else MISSING
    return [
        f"### {markdown_cell(name)}",
        "",
        f"- 井段：{_interval(layer)}",
        f"- 岩性：{markdown_cell(layer.lithology)}",
        f"- 储层特征：{markdown_cell(layer.reservoir_class)}；关键参数：{traits}",
        f"- 含油气特征：流体类型{markdown_cell(layer.fluid_type)}，"
        f"含水饱和度{measurement(layer.water_saturation)}，"
        f"含油饱和度{measurement(layer.hydrocarbon_saturation)}",
        f"- 综合解释：{markdown_cell(layer.layer_type)}",
        f"- 解释依据：{evidence}",
        f"- 建议：{markdown_cell(layer.recommendation)}",
        "",
    ]


def _overall(result: NormalizedInterpretationResult) -> str:
    if not result.layers:
        return "本次未形成可汇总的结构化解释层段。"
    counts = Counter(layer.layer_type for layer in result.layers if layer.layer_type)
    types = "、".join(f"{key}{value}层" for key, value in counts.items()) or MISSING
    effective = [
        layer.effective_thickness_m
        for layer in result.layers
        if layer.effective_thickness_m is not None
    ]
    thickness = f"已提供层段的有效厚度合计{number(sum(effective))} m。" if effective else ""
    return (
        f"本次共形成{len(result.layers)}个解释层段，解释类型统计为{types}。"
        f"主要岩性包括{'、'.join(result.lithology_summary) or MISSING}；"
        f"储层评价包括{'、'.join(result.reservoir_summary) or MISSING}。{thickness}"
    )


def _summary_sentence(prefix: str, values: tuple[str, ...]) -> str:
    return (
        f"{prefix}{'、'.join(map(markdown_cell, values))}。" if values else f"{prefix}{MISSING}。"
    )


def _classification_sentence(layers: tuple[InterpretedLayer, ...]) -> str:
    values = tuple(dict.fromkeys(layer.layer_type for layer in layers if layer.layer_type))
    return _summary_sentence("本井实际形成的层类型为", values)


def _interval(layer: InterpretedLayer) -> str:
    if layer.top_depth_m is None or layer.bottom_depth_m is None:
        return MISSING
    return f"{number(layer.top_depth_m)}～{number(layer.bottom_depth_m)} m"


def _depth(value: float | None) -> str:
    rendered = number(value)
    return rendered if rendered == MISSING else f"{rendered} m"
