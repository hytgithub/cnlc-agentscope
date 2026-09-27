"""严格结构化输入与不完整计划；不识别自然语言，不调用模型或业务执行层。"""

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from cnlc_agent.demo.operation_models import (
    ActionType,
    ExecutionReference,
    InputClassification,
    NonBlank,
    OperationCondition,
    OperationConstraint,
    OperationContext,
    OperationEdge,
    OperationInputReference,
    OperationNode,
    OperationParameters,
    OperationPlan,
    OperationScope,
    OutputRequirement,
    PersistMode,
    TargetType,
)
from cnlc_agent.demo.task_context import TaskReference
from cnlc_agent.domain.models import Contract, JsonObject


class ClarificationSlot(StrEnum):
    """尚未补齐的语义槽位；方法与模型共用 METHOD，具体字段由动作区分。"""

    TASK = "TASK"
    EXECUTION = "EXECUTION"
    SCOPE = "SCOPE"
    TARGET = "TARGET"
    VALUE = "VALUE"
    METHOD = "METHOD"
    COMPARE_TARGET = "COMPARE_TARGET"
    PERSIST_MODE = "PERSIST_MODE"
    CONFLICT_RESOLUTION = "CONFLICT_RESOLUTION"


class ClarificationIssue(Contract):
    """可定位到操作的澄清原因；evidence 仅为解释证据，不参与授权。"""

    operation_id: NonBlank | None = None
    slot: ClarificationSlot
    error_code: NonBlank = "CLARIFICATION_REQUIRED"
    message: NonBlank
    evidence: JsonObject = Field(default_factory=dict)


class PartialOperationNode(Contract):
    """动作已知但目标等槽位可以缺失的中间节点，不能直接传入业务 Tool。"""

    operation_id: NonBlank
    action: ActionType
    target: TargetType | None = None
    task_reference: TaskReference | None = None
    execution_reference: ExecutionReference | None = None
    scope: OperationScope | None = None
    parameters: OperationParameters = Field(default_factory=OperationParameters)
    constraints: list[OperationConstraint] = Field(default_factory=list)
    input_refs: list[OperationInputReference] = Field(default_factory=list)
    output_alias: NonBlank | None = None

    @model_validator(mode="after")
    def reject_shadow_fields(self) -> Self:
        """扩展载荷不得藏匿正式字段或自称可信引用。"""

        check_extensions(self.parameters.extensions)
        return self


# 保留 A 的完整模型，不通过继承覆盖必填字段来放宽其约束。
class PartialOperationPlan(Contract):
    """解析和澄清中间态；完整计划的 persist_mode 仍然必填。"""

    input_classification: InputClassification
    shared_context: OperationContext = Field(default_factory=OperationContext)
    operations: list[PartialOperationNode] = Field(default_factory=list)
    edges: list[OperationEdge] = Field(default_factory=list)
    conditions: list[OperationCondition] = Field(default_factory=list)
    persist_mode: PersistMode | None = None
    output_requirement: OutputRequirement | None = None
    original_instruction: NonBlank


def check_extensions(extensions: JsonObject) -> None:
    """扩展只保存非正式元数据；任何层级都不能伪装正式输入或锁定证明。"""

    reserved = (
        set(OperationNode.model_fields)
        | set(OperationPlan.model_fields)
        | set(OperationParameters.model_fields)
        | {
            "trusted",
            "locked",
            "locked_references",
            "owner_token",
            "task_id",
            "execution_id",
            "interval_id",
            "persist_mode",
        }
    )

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if reserved.intersection(value):
                raise ValueError("extensions cannot shadow formal fields or trusted references")
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(extensions)


class StructuredOperationParser:
    """只接收 dict/JSON；未知字段、非法枚举、未知动作交由严格 Schema 拒绝。"""

    @staticmethod
    def parse(payload: dict[str, object] | str) -> PartialOperationPlan:
        """解析结构化语义，不扫描中文关键词或推断省略字段。"""

        if isinstance(payload, str):
            return PartialOperationPlan.model_validate_json(payload)
        return PartialOperationPlan.model_validate(payload)


class FinalizationResult(Contract):
    """补齐结果；完整 Schema 构造成功仍须经过全计划校验和引用授权。"""

    plan: OperationPlan | None = None
    issues: list[ClarificationIssue] = Field(default_factory=list)


def missing_plan_slots(plan: PartialOperationPlan) -> list[ClarificationIssue]:
    """检查明确必需的语义字段，不猜目标、保存模式或比较对象。"""

    issues: list[ClarificationIssue] = []
    if plan.persist_mode is None:
        issues.append(
            ClarificationIssue(
                slot=ClarificationSlot.PERSIST_MODE, message="请明确预览还是创建正式版本"
            )
        )
    for op in plan.operations:
        missing: list[tuple[ClarificationSlot, str, str]] = []
        if op.target is None:
            missing.append((ClarificationSlot.TARGET, "CLARIFICATION_REQUIRED", "请明确操作目标"))
        if op.action in {ActionType.MODIFY_RESULT, ActionType.MODIFY_PARAMETER}:
            # 模型参数使用标识，不伪造数值；其他修改仍必须明确 ValueSpec。
            model_parameter = op.action == ActionType.MODIFY_PARAMETER and (
                op.parameters.parameter_name == "prediction_model"
                or (op.parameters.parameter_name is None and op.target == TargetType.MODEL)
            )
            if model_parameter and op.parameters.model_id is None:
                missing.append(
                    (ClarificationSlot.METHOD, "CLARIFICATION_REQUIRED", "请明确预测模型标识")
                )
            elif not model_parameter and op.parameters.value is None:
                missing.append((ClarificationSlot.VALUE, "CLARIFICATION_REQUIRED", "请明确修改值"))
        if (op.action == ActionType.SWITCH_METHOD and op.parameters.method_id is None) or (
            op.action == ActionType.SWITCH_MODEL and op.parameters.model_id is None
        ):
            missing.append(
                (ClarificationSlot.METHOD, "CLARIFICATION_REQUIRED", "请明确方法或模型标识")
            )
        if op.action == ActionType.COMPARE:
            if len({ref.model_dump_json() for ref in op.input_refs}) < 2:
                missing.append(
                    (
                        ClarificationSlot.COMPARE_TARGET,
                        "COMPARE_TARGET_REQUIRED",
                        "比较至少需要两个明确输入对象",
                    )
                )
        issues.extend(
            ClarificationIssue(
                operation_id=op.operation_id, slot=slot, error_code=code, message=message
            )
            for slot, code, message in missing
        )
    return issues


def finalize_partial_plan(plan: PartialOperationPlan) -> FinalizationResult:
    """仅在必需槽位齐全时生成严格完整计划；无数据库查询或业务副作用。"""

    checked = PartialOperationPlan.model_validate(plan.model_dump(mode="json"))
    issues = missing_plan_slots(checked)
    if issues:
        return FinalizationResult(issues=issues)
    return FinalizationResult(plan=OperationPlan.model_validate(checked.model_dump(mode="json")))
