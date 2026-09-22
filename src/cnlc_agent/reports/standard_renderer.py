"""正式、纯文本的测井评价报告模板。"""

from collections import Counter

from cnlc_agent.reports.formatter import MISSING, markdown_cell, measurement, number
from cnlc_agent.reports.models import InterpretedLayer, NormalizedInterpretationResult


class StandardReportRenderer:
    """按正式报告章节生成详细版 Markdown。"""

    def render(self, result: NormalizedInterpretationResult) -> str:
        """把只读归一化结果渲染为标准报告，不新增专业结论。"""

        well = result.well
        title = well.name or well.well_id
        lines = [
            f"# {markdown_cell(title)}测井评价报告",
            "",
            "编 写 人：测井解释智能体  ",
            f"校 对 人：{MISSING}  ",
            f"审 核 人：{MISSING}  ",
            "",
            f"日期：{markdown_cell(result.generated_date)}",
            "",
        ]
        if result.is_mock:
            lines.extend(
                [
                    "> Demo / Mock 演示报告：专业参数和结论来自结构化演示资料，",
                    "> 不代表经过独立专业复算与验证的真实测井评价结论。",
                    "",
                ]
            )
        lines.extend(
            [
                "# 目 录",
                "",
                "一、钻井与地质概况  ",
                "1、钻井概况  ",
                "2、地质概况  ",
                "",
                "二、测井采集资料质量评价  ",
                "1、采集概况  ",
                "2、采集质量评价  ",
                "",
                "三、快速处理与解释  ",
                "1、资料预处理  ",
                "2、主要目的层处理解释成果  ",
                "3、测试建议  ",
                "",
                "四、精细解释与评价  ",
                "1、岩石物理分析  ",
                "2、综合评价  ",
                "",
                "五、存在问题及建议",
                "",
                "# 一、钻井与地质概况",
                "",
                "## 1、钻井概况",
                "",
                _drilling_narrative(result),
                "",
                "| 项目 | 内容 |",
                "|---|---|",
                f"| 井名 | {markdown_cell(well.name)} |",
                f"| 井号 | {markdown_cell(well.well_id)} |",
                f"| 井别 | {markdown_cell(well.well_category)} |",
                f"| 井型 | {markdown_cell(well.well_type)} |",
                f"| 完钻日期 | {markdown_cell(well.completion_date)} |",
                f"| 完钻井深 | {_depth_with_unit(well.total_depth_m)} |",
                f"| 完钻层位 | {markdown_cell(well.completion_formation)} |",
                f"| 构造位置 | {markdown_cell(well.structural_location)} |",
                f"| 测井井段 | {markdown_cell(well.logging_interval)} |",
                f"| 钻探目的 | {markdown_cell(well.drilling_purpose)} |",
                f"| 井身结构 | {markdown_cell(well.wellbore_structure)} |",
                f"| 钻井液性质 | {markdown_cell(well.drilling_fluid)} |",
                f"| 其他说明 | {markdown_cell(well.other_notes)} |",
                "",
                "## 2、地质概况",
                "",
            ]
        )
        lines.extend(_paragraphs_or_missing(result.geology_summary, "本次未提供完整地质概况资料。"))
        lines.extend(["", "### 录井显示", ""])
        lines.append(
            markdown_cell(result.mud_logging_summary)
            if result.mud_logging_summary
            else "本次未提供完整录井显示资料。"
        )
        lines.extend(["", "### 岩心资料", ""])
        lines.append(
            markdown_cell(result.core_summary)
            if result.core_summary
            else "本次未提供岩心取心及实验分析资料。"
        )
        lines.extend(
            [
                "",
                "# 二、测井采集资料质量评价",
                "",
                "## 1、采集概况",
                "",
                _acquisition_narrative(result),
                "",
                "| 测井项目 | 测量井段 | 数据状态 | 主要用途 |",
                "|---|---|---|---|",
            ]
        )
        if result.curves:
            for curve in result.curves:
                status = f"有效{curve.valid_samples}/{curve.total_samples}点"
                lines.append(
                    f"| {markdown_cell(curve.name)} ({markdown_cell(curve.unit)}) "
                    f"| {markdown_cell(curve.interval)} | {status} "
                    f"| {markdown_cell(curve.purpose)} |"
                )
        else:
            lines.append(f"| {MISSING} | {MISSING} | {MISSING} | {MISSING} |")
        lines.extend(["", "## 2、采集质量评价", ""])
        lines.extend(_paragraphs_or_missing(result.quality_summary, "本次未形成结构化质量评价。"))
        if result.quality_evidence:
            lines.extend(["", "质量评价依据：", ""])
            lines.extend(f"- {markdown_cell(item)}" for item in result.quality_evidence)

        lines.extend(
            [
                "",
                "# 三、快速处理与解释",
                "",
                "## 1、资料预处理",
                "",
            ]
        )
        lines.extend(f"- {markdown_cell(item)}" for item in result.preprocessing)
        lines.extend(
            [
                "",
                "## 2、主要目的层处理解释成果",
                "",
                "| 层号 | 层位 | 起始深度(m) | 终止深度(m) | 厚度(m) | 电阻率 "
                "| 声波时差 | 密度 | 孔隙度 | 渗透率 | 含油饱和度 | 结论 |",
                "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        lines.extend(_standard_layer_rows(result.layers))
        lines.extend(
            [
                "",
                "## 3、测试建议",
                "",
                "测试建议仅采用结构化解释结果中已经给出的建议，不因报告模板自动新增试油结论。",
                "",
                "| 层位 | 井段(m) | 解释结论 | 建议 |",
                "|---|---|---|---|",
            ]
        )
        if result.layers:
            for layer in result.layers:
                lines.append(
                    f"| {markdown_cell(_layer_name(layer))} | {markdown_cell(_interval(layer))} "
                    f"| {markdown_cell(layer.layer_type)} | {markdown_cell(layer.recommendation)} |"
                )
        else:
            lines.append(f"| {MISSING} | {MISSING} | {MISSING} | {MISSING} |")
        if result.recommendations:
            lines.extend(["", *_numbered(result.recommendations)])
        else:
            lines.extend(["", "现有结构化结果未提供测试建议，需结合后续验证资料确定。"])

        lines.extend(
            [
                "",
                "# 四、精细解释与评价",
                "",
                "## 1、岩石物理分析",
                "",
            ]
        )
        if result.core_summary:
            lines.append(f"岩心资料摘要：{markdown_cell(result.core_summary)}")
        else:
            lines.append(
                "本次未提供岩心实验分析资料，本节仅展示现有测井解释参数，不作新的岩石物理计算。"
            )
        lines.extend(
            [
                "",
                "| 层位 | 始深(m) | 终深(m) | 厚度(m) | 密度 | 孔隙度 "
                "| 渗透率 | 含水饱和度 | 含油饱和度 | 评价 |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        lines.extend(_petrophysics_rows(result.layers))
        lines.extend(["", "## 2、综合评价", ""])
        if result.layers:
            for layer in result.layers:
                lines.extend(
                    [f"### {markdown_cell(_layer_name(layer))}", "", _layer_evaluation(layer), ""]
                )
        else:
            lines.append("本次未形成可供逐层评价的结构化解释层段。")
        lines.extend(_overall_evaluation(result))

        lines.extend(["", "# 五、存在问题及建议", ""])
        if result.limitations:
            lines.extend(_numbered(result.limitations))
        else:
            lines.append("现有资料基本满足本次解释需要，未发现明显影响主要解释结论的问题。")
        if result.validation.summary:
            lines.extend(["", f"综合验证结论：{markdown_cell(result.validation.summary)}"])
        if result.validation.recommended_action:
            lines.append(f"验证建议：{markdown_cell(result.validation.recommended_action)}")
        return "\n".join(lines).rstrip() + "\n"


def _drilling_narrative(result: NormalizedInterpretationResult) -> str:
    well = result.well
    facts = [f"{well.name or well.well_id}（井号：{well.well_id}）"]
    if well.well_category:
        facts.append(f"井别为{well.well_category}")
    if well.well_type:
        facts.append(f"井型为{well.well_type}")
    if well.total_depth_m is not None:
        facts.append(f"完钻井深{number(well.total_depth_m)} m")
    if well.completion_formation:
        facts.append(f"完钻层位{well.completion_formation}")
    if well.structural_location:
        facts.append(f"构造位置位于{well.structural_location}")
    sentence = "，".join(facts) + "。"
    if well.drilling_purpose:
        sentence += f"本次解释目的为：{well.drilling_purpose}。"
    return markdown_cell(sentence)


def _acquisition_narrative(result: NormalizedInterpretationResult) -> str:
    if not result.curve_names:
        return "本次未提供可识别的常规测井曲线清单。"
    return f"本井本次解释使用的测井曲线包括：{'、'.join(map(markdown_cell, result.curve_names))}。"


def _standard_layer_rows(layers: tuple[InterpretedLayer, ...]) -> list[str]:
    if not layers:
        return ["| " + " | ".join([MISSING] * 12) + " |"]
    return [
        f"| {markdown_cell(layer.layer_id)} | {markdown_cell(layer.formation)} "
        f"| {number(layer.top_depth_m)} | {number(layer.bottom_depth_m)} "
        f"| {number(layer.gross_thickness_m)} | {measurement(layer.resistivity)} "
        f"| {measurement(layer.acoustic)} | {measurement(layer.density)} "
        f"| {measurement(layer.porosity)} | {measurement(layer.permeability)} "
        f"| {measurement(layer.hydrocarbon_saturation)} | {markdown_cell(layer.layer_type)} |"
        for layer in layers
    ]


def _petrophysics_rows(layers: tuple[InterpretedLayer, ...]) -> list[str]:
    if not layers:
        return ["| " + " | ".join([MISSING] * 10) + " |"]
    return [
        f"| {markdown_cell(_layer_name(layer))} | {number(layer.top_depth_m)} "
        f"| {number(layer.bottom_depth_m)} | {number(layer.gross_thickness_m)} "
        f"| {measurement(layer.density)} | {measurement(layer.porosity)} "
        f"| {measurement(layer.permeability)} | {measurement(layer.water_saturation)} "
        f"| {measurement(layer.hydrocarbon_saturation)} | {markdown_cell(layer.reservoir_class)} |"
        for layer in layers
    ]


def _layer_evaluation(layer: InterpretedLayer) -> str:
    name = _layer_name(layer)
    subject = "该解释层段" if name == MISSING else name
    sentences = [f"{subject}，井段{_interval(layer)}。"]
    if layer.effective_thickness_m is not None:
        sentences.append(f"有效厚度{number(layer.effective_thickness_m)} m。")
    traits = []
    if layer.lithology:
        traits.append(f"岩性为{layer.lithology}")
    if layer.reservoir_class:
        traits.append(f"储层评价为{layer.reservoir_class}")
    if traits:
        sentences.append("，".join(traits) + "。")
    parameters = _parameter_phrases(layer)
    if parameters:
        sentences.append(f"现有关键参数为{'，'.join(parameters)}。")
    fluid = []
    if layer.fluid_type:
        fluid.append(f"流体识别为{layer.fluid_type}")
    if measurement(layer.water_saturation) != MISSING:
        fluid.append(f"含水饱和度{measurement(layer.water_saturation)}")
    if measurement(layer.hydrocarbon_saturation) != MISSING:
        fluid.append(f"含油饱和度{measurement(layer.hydrocarbon_saturation)}")
    if fluid:
        sentences.append("，".join(fluid) + "。")
    sentences.append(f"综合解释结论为{layer.layer_type or MISSING}。")
    if layer.recommendation:
        sentences.append(f"已有建议：{layer.recommendation}。")
    return markdown_cell("".join(sentences))


def _parameter_phrases(layer: InterpretedLayer) -> list[str]:
    values = [
        ("电阻率", layer.resistivity),
        ("声波时差", layer.acoustic),
        ("密度", layer.density),
        ("孔隙度", layer.porosity),
        ("渗透率", layer.permeability),
    ]
    return [
        f"{label}{rendered}"
        for label, value in values
        if (rendered := measurement(value)) != MISSING
    ]


def _overall_evaluation(result: NormalizedInterpretationResult) -> list[str]:
    if not result.layers:
        return []
    counts = Counter(layer.layer_type for layer in result.layers if layer.layer_type)
    count_text = "、".join(f"{key}{value}层" for key, value in counts.items()) or MISSING
    effective = [
        layer.effective_thickness_m
        for layer in result.layers
        if layer.effective_thickness_m is not None
    ]
    effective_text = f"已提供层段的有效厚度合计为{number(sum(effective))} m。" if effective else ""
    return [
        "整井结构化解释结果共包含"
        f"{len(result.layers)}个解释层段，分类统计为{count_text}。{effective_text}"
    ]


def _layer_name(layer: InterpretedLayer) -> str:
    values = [value for value in (layer.formation, layer.layer_id) if value]
    return " ".join(values) if values else MISSING


def _interval(layer: InterpretedLayer) -> str:
    if layer.top_depth_m is None or layer.bottom_depth_m is None:
        return MISSING
    return f"{number(layer.top_depth_m)}～{number(layer.bottom_depth_m)}"


def _depth_with_unit(value: float | None) -> str:
    rendered = number(value)
    return rendered if rendered == MISSING else f"{rendered} m"


def _paragraphs_or_missing(values: tuple[str, ...], fallback: str) -> list[str]:
    return [markdown_cell(value) for value in values] if values else [fallback]


def _numbered(values: tuple[str, ...]) -> list[str]:
    return [f"{index}. {markdown_cell(value)}" for index, value in enumerate(values, 1)]
