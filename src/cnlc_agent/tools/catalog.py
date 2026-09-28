"""现有 Tool Code 到 Workflow Step 和业务阶段的静态能力契约。"""

from collections.abc import Mapping
from typing import Protocol

from pydantic import Field, model_validator

from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.errors import ToolError
from cnlc_agent.domain.models import Contract
from cnlc_agent.domain.stages import STAGE_STEPS, InterpretationStage


class ToolDescriptor(Protocol):
    """组合根校验只需要 Tool 暴露稳定名称。"""

    @property
    def name(self) -> str: ...


class ToolBinding(Contract):
    """Tool 的稳定业务归属；不表示运行记录或新的版本体系。"""

    tool_code: str = Field(min_length=1, max_length=128)
    allowed_steps: tuple[StepId, ...] = Field(min_length=1)
    business_stage: InterpretationStage
    description: str = Field(min_length=1, max_length=256)
    provider_batch: bool = False

    @model_validator(mode="after")
    def validate_stage_steps(self) -> "ToolBinding":
        """Catalog 自身必须服从 Task 11A 已冻结的 Step→Stage 映射。"""

        if len(self.allowed_steps) != len(set(self.allowed_steps)):
            raise ValueError("tool allowed steps must be unique")
        if any(step not in STAGE_STEPS[self.business_stage] for step in self.allowed_steps):
            raise ValueError("tool allowed step does not belong to business stage")
        return self


BUSINESS_TOOL_BINDINGS = (
    ToolBinding(
        tool_code="get_well_data",
        allowed_steps=(StepId.W01,),
        business_stage=InterpretationStage.DECODE,
        description="读取井、原始曲线和资料要求",
    ),
    ToolBinding(
        tool_code="check_curve_quality",
        allowed_steps=(StepId.W03,),
        business_stage=InterpretationStage.PREPROCESS,
        description="执行曲线质量检查并返回 QC 结果",
    ),
    ToolBinding(
        tool_code="identify_lithology",
        allowed_steps=(StepId.W04,),
        business_stage=InterpretationStage.INTERPRET,
        description="返回岩性识别结果",
    ),
    ToolBinding(
        tool_code="evaluate_petrophysics",
        allowed_steps=(StepId.W05,),
        business_stage=InterpretationStage.INTERPRET,
        description="返回现有储层物性评价结果",
    ),
    ToolBinding(
        tool_code="calculate_sw",
        allowed_steps=(StepId.W06,),
        business_stage=InterpretationStage.INTERPRET,
        description="返回已有含水饱和度计算结果",
    ),
    ToolBinding(
        tool_code="identify_fluid",
        allowed_steps=(StepId.W06,),
        business_stage=InterpretationStage.INTERPRET,
        description="company profile 的流体识别结果投影",
    ),
    ToolBinding(
        tool_code="classify_layer",
        allowed_steps=(StepId.W07,),
        business_stage=InterpretationStage.INTERPRET,
        description="company profile 的油气水层分类结果投影",
    ),
    ToolBinding(
        tool_code="merge_intervals",
        allowed_steps=(StepId.W08,),
        business_stage=InterpretationStage.INTERPRET,
        description="返回已有层段整理结果",
    ),
    ToolBinding(
        tool_code="validate_interpretation",
        allowed_steps=(StepId.W09,),
        business_stage=InterpretationStage.INTERPRET,
        description="company profile 的综合验证结果投影",
    ),
    ToolBinding(
        tool_code="prepare_report",
        allowed_steps=(StepId.W10,),
        business_stage=InterpretationStage.INTERPRET,
        description="W10 final-check/provider projection，不生成业务报告",
    ),
)

PROVIDER_BATCH_BINDINGS = (
    ToolBinding(
        tool_code="company_analysis",
        allowed_steps=(StepId.W01,),
        business_stage=InterpretationStage.DECODE,
        description="company analysis provider batch 的一次物理调用",
        provider_batch=True,
    ),
    ToolBinding(
        tool_code="company_preprocessing",
        allowed_steps=(StepId.W03,),
        business_stage=InterpretationStage.PREPROCESS,
        description="company preprocessing provider batch 的一次物理调用",
        provider_batch=True,
    ),
    ToolBinding(
        tool_code="company_interpretation",
        allowed_steps=(
            StepId.W04,
            StepId.W05,
            StepId.W06,
            StepId.W07,
            StepId.W08,
            StepId.W09,
        ),
        business_stage=InterpretationStage.INTERPRET,
        description="company interpretation provider batch 的一次物理调用",
        provider_batch=True,
    ),
    ToolBinding(
        tool_code="company_report",
        allowed_steps=(StepId.W10,),
        business_stage=InterpretationStage.INTERPRET,
        description="W10 使用的 company report provider batch，不是业务 REPORT 阶段",
        provider_batch=True,
    ),
)

TOOL_BINDINGS = (*BUSINESS_TOOL_BINDINGS, *PROVIDER_BATCH_BINDINGS)
_BINDING_BY_CODE = {binding.tool_code: binding for binding in TOOL_BINDINGS}
if len(_BINDING_BY_CODE) != len(TOOL_BINDINGS):
    raise RuntimeError("tool catalog contains duplicate tool codes")


def tool_binding(tool_code: str) -> ToolBinding | None:
    """按 Tool Code 查询静态能力；未知扩展不在这里自动注册。"""

    return _BINDING_BY_CODE.get(tool_code)


def validate_tool_step(tool_code: str, step_id: StepId) -> None:
    """已登记 Tool 只能在声明步骤调用；独立测试扩展保持原兼容语义。"""

    binding = tool_binding(tool_code)
    if binding is not None and step_id not in binding.allowed_steps:
        raise ToolError(
            "TOOL_STEP_MISMATCH",
            f"工具 {tool_code} 不允许在步骤 {step_id.value} 调用",
        )


def validate_injected_tools(tools: Mapping[str, ToolDescriptor]) -> None:
    """组合根只允许注入 Catalog 已登记且键名一致的业务 Tool。"""

    for key, tool in tools.items():
        if key != tool.name:
            raise ValueError(f"tool registry key does not match tool name: {key}")
        if tool_binding(tool.name) is None:
            raise ValueError(f"tool is not registered in catalog: {tool.name}")
