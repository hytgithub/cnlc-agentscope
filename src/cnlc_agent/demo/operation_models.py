"""有限的操作语义契约；只描述意图，不解析引用、不授权也不创建 Execution。

Operation 可以组合为同一次业务执行，也可以只读；本模块不规定节点与执行的数量关系。
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from cnlc_agent.demo.task_context import TaskReference
from cnlc_agent.domain.models import Contract, JsonObject

NonBlank = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


class InputClassification(StrEnum):
    """输入的交互性质；能力询问与执行请求必须分开表达。"""

    EXECUTION_REQUEST = "EXECUTION_REQUEST"
    READ_REQUEST = "READ_REQUEST"
    CLARIFICATION_REPLY = "CLARIFICATION_REPLY"
    CORRECTION = "CORRECTION"
    CONFIRMATION = "CONFIRMATION"
    CANCELLATION = "CANCELLATION"
    CAPABILITY_QUERY = "CAPABILITY_QUERY"
    META_REQUEST = "META_REQUEST"
    OUT_OF_DOMAIN = "OUT_OF_DOMAIN"


class ActionType(StrEnum):
    """系统可识别的业务动作；是否开放另由 Capability 决定。"""

    QUERY = "QUERY"
    EXPLAIN = "EXPLAIN"
    MODIFY_RESULT = "MODIFY_RESULT"
    MODIFY_PARAMETER = "MODIFY_PARAMETER"
    RECALCULATE = "RECALCULATE"
    REINTERPRET = "REINTERPRET"
    SWITCH_METHOD = "SWITCH_METHOD"
    SWITCH_MODEL = "SWITCH_MODEL"
    COMPARE = "COMPARE"
    SEGMENT_EDIT = "SEGMENT_EDIT"
    VALIDATE = "VALIDATE"
    OVERRIDE = "OVERRIDE"
    RESTORE_VERSION = "RESTORE_VERSION"
    SCENARIO = "SCENARIO"
    COMMIT_SCENARIO = "COMMIT_SCENARIO"
    REPORT = "REPORT"
    HISTORY = "HISTORY"
    FULL_INTERPRET = "FULL_INTERPRET"
    FULL_RERUN = "FULL_RERUN"
    STATUS = "STATUS"
    CANCEL_EXECUTION = "CANCEL_EXECUTION"
    PAUSE_EXECUTION = "PAUSE_EXECUTION"
    RESUME_EXECUTION = "RESUME_EXECUTION"
    RETRY_EXECUTION = "RETRY_EXECUTION"
    ACCEPT_RESULT = "ACCEPT_RESULT"
    REJECT_RESULT = "REJECT_RESULT"
    MARK_FINAL = "MARK_FINAL"
    MARK_REVIEW = "MARK_REVIEW"
    NEW_WELL = "NEW_WELL"
    REPLACE_INPUT = "REPLACE_INPUT"
    ADD_EVIDENCE = "ADD_EVIDENCE"


class TargetType(StrEnum):
    """操作对象类型；不将层号、深度或范围编码进对象名称。"""

    WELL = "WELL"
    RAW_CURVE = "RAW_CURVE"
    LITHOLOGY = "LITHOLOGY"
    VSH = "VSH"
    POROSITY = "POROSITY"
    PERMEABILITY = "PERMEABILITY"
    WATER_SATURATION = "WATER_SATURATION"
    FLUID = "FLUID"
    ZONE_CLASSIFICATION = "ZONE_CLASSIFICATION"
    INTERVAL = "INTERVAL"
    LAYER_BOUNDARY = "LAYER_BOUNDARY"
    RW = "RW"
    ARCHIE_PARAMETER = "ARCHIE_PARAMETER"
    CUTOFF = "CUTOFF"
    MODEL = "MODEL"
    METHOD = "METHOD"
    EVIDENCE = "EVIDENCE"
    REPORT = "REPORT"
    EXECUTION = "EXECUTION"


class OperationScopeKind(StrEnum):
    """范围的判别值；各范围仅接受自身需要的字段。"""

    WHOLE_WELL = "WHOLE_WELL"
    INTERVAL = "INTERVAL"
    MULTI_INTERVAL = "MULTI_INTERVAL"
    DEPTH_RANGE = "DEPTH_RANGE"
    DEPTH_POINT = "DEPTH_POINT"
    FILTER_SET = "FILTER_SET"


class DepthReference(StrEnum):
    """显式深度基准；仅建模，不提供基准转换。"""

    MD = "MD"
    TVD = "TVD"
    TVDSS = "TVDSS"


class WholeWellScope(Contract):
    """整井范围；不能附带局部范围字段。"""

    kind: Literal[OperationScopeKind.WHOLE_WELL] = OperationScopeKind.WHOLE_WELL


class IntervalScope(Contract):
    """使用稳定层段标识；自然语言层号留给后续 Resolver。"""

    kind: Literal[OperationScopeKind.INTERVAL] = OperationScopeKind.INTERVAL
    interval_id: NonBlank


class MultiIntervalScope(Contract):
    """明确的非空层段集合。"""

    kind: Literal[OperationScopeKind.MULTI_INTERVAL] = OperationScopeKind.MULTI_INTERVAL
    interval_ids: list[NonBlank] = Field(min_length=1)


class DepthRangeScope(Contract):
    """以米表示的连续深度区间；只校验几何顺序，不补造业务阈值。"""

    kind: Literal[OperationScopeKind.DEPTH_RANGE] = OperationScopeKind.DEPTH_RANGE
    top: float = Field(strict=True)
    bottom: float = Field(strict=True)
    depth_reference: DepthReference
    unit: Literal["m"] = "m"

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        """拒绝零厚度及倒置区间，允许有符号的海拔基准深度。"""

        if self.top >= self.bottom:
            raise ValueError("top must be less than bottom")
        return self


class DepthPointScope(Contract):
    """一个显式深度点，不隐含采样吸附或插值规则。"""

    kind: Literal[OperationScopeKind.DEPTH_POINT] = OperationScopeKind.DEPTH_POINT
    depth: float = Field(strict=True)
    depth_reference: DepthReference
    unit: Literal["m"] = "m"


class FilterSetScope(Contract):
    """保存筛选描述与冻结后的对象集合；None 表示尚未解析，空列表表示无匹配。"""

    kind: Literal[OperationScopeKind.FILTER_SET] = OperationScopeKind.FILTER_SET
    filter_expression: NonBlank
    resolved_ids: list[NonBlank] | None = None


# 判别联合使 JSON Schema 和运行时同时拒绝范围字段混用。
OperationScope = Annotated[
    WholeWellScope
    | IntervalScope
    | MultiIntervalScope
    | DepthRangeScope
    | DepthPointScope
    | FilterSetScope,
    Field(discriminator="kind"),
]


class ExecutionReferenceKind(StrEnum):
    """版本引用语义；任务最新版本与用户工作基线保持独立。"""

    TASK_CURRENT = "TASK_CURRENT"
    ACTIVE_BASE = "ACTIVE_BASE"
    PREVIOUS = "PREVIOUS"
    LATEST_SUCCESSFUL = "LATEST_SUCCESSFUL"
    FIRST = "FIRST"
    SEQUENCE = "SEQUENCE"
    EXECUTION_ID = "EXECUTION_ID"


class ExecutionReference(Contract):
    """未解析的版本引用，不据此查询数据库或证明任务归属。"""

    kind: ExecutionReferenceKind
    sequence: int | None = Field(default=None, gt=0, strict=True)
    execution_id: NonBlank | None = None

    @model_validator(mode="after")
    def validate_selector(self) -> Self:
        """序号、ID 与符号引用互斥，禁止静默丢弃多余选择条件。"""

        if (self.kind == ExecutionReferenceKind.SEQUENCE) != (self.sequence is not None):
            raise ValueError("sequence is required only for SEQUENCE")
        if (self.kind == ExecutionReferenceKind.EXECUTION_ID) != (self.execution_id is not None):
            raise ValueError("execution_id is required only for EXECUTION_ID")
        return self


class ValueMode(StrEnum):
    """绝对设置、绝对增量和相对变化有不同计算含义。"""

    ABSOLUTE = "ABSOLUTE"
    DELTA = "DELTA"
    PERCENT_CHANGE = "PERCENT_CHANGE"


class ValueSpec(Contract):
    """保留数值与显式单位，不做换算或专业取值范围推断。

    例如绝对 16% 为 ABSOLUTE/16/%，提高 2 个百分点为 DELTA/2/percentage_point，
    相对提高 2% 为 PERCENT_CHANGE/2/%。单位字符串暂不建立专业单位注册表。
    """

    mode: ValueMode
    value: float = Field(strict=True)
    unit: NonBlank


class PersistMode(StrEnum):
    """临时预览或追加正式版本；没有原地覆盖历史的模式。"""

    PREVIEW = "PREVIEW"
    CREATE_VERSION = "CREATE_VERSION"


class OperationConstraintType(StrEnum):
    """用户明确提出的限制；本阶段只记录，不推断执行范围。"""

    ONLY_SCOPE = "ONLY_SCOPE"
    DO_NOT_PERSIST = "DO_NOT_PERSIST"
    EXCLUDE_MODEL = "EXCLUDE_MODEL"
    USE_RULE_ONLY = "USE_RULE_ONLY"
    KEEP_UNAFFECTED_RESULTS = "KEEP_UNAFFECTED_RESULTS"
    NO_FULL_RERUN = "NO_FULL_RERUN"
    EXCLUDE_SCOPE = "EXCLUDE_SCOPE"


class OperationConstraint(Contract):
    """可组合限制；排除模型和排除范围必须携带明确对象。"""

    type: OperationConstraintType
    model_id: NonBlank | None = None
    scope: OperationScope | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        """禁止带参约束缺参，以及无关参数被静默接受。"""

        if (self.type == OperationConstraintType.EXCLUDE_MODEL) != (self.model_id is not None):
            raise ValueError("model_id is required only for EXCLUDE_MODEL")
        if (self.type == OperationConstraintType.EXCLUDE_SCOPE) != (self.scope is not None):
            raise ValueError("scope is required only for EXCLUDE_SCOPE")
        return self


class OperationEdgeType(StrEnum):
    """节点之间的语义关系，不是专业计算依赖图。"""

    SEQUENCE = "SEQUENCE"
    DATA_DEPENDENCY = "DATA_DEPENDENCY"
    COMPARE_DEPENDENCY = "COMPARE_DEPENDENCY"


class OperationEdge(Contract):
    """显式节点关系；端点存在性和环检测留给后续 PlanValidator。"""

    from_operation_id: NonBlank
    to_operation_id: NonBlank
    type: OperationEdgeType


class OperationInputReference(Contract):
    """引用其他操作的输出或某个版本；不把操作输出伪装成 Execution。"""

    operation_id: NonBlank | None = None
    execution_reference: ExecutionReference | None = None
    task_reference: TaskReference | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        """一种输入只能来自一个来源；操作引用无需另带任务选择器。"""

        if (self.operation_id is None) == (self.execution_reference is None):
            raise ValueError("choose exactly one operation or execution reference")
        if self.operation_id is not None and self.task_reference is not None:
            raise ValueError("task_reference is only valid with execution_reference")
        return self


class OperationParameters(Contract):
    """语义参数；既有 InterpretationOverride 仍是实际修改命令的唯一契约。

    尚未冻结的动作载荷可存入 extensions，不能把其中内容直接作为 Tool 参数执行。
    """

    value: ValueSpec | None = None
    parameter_name: NonBlank | None = None
    method_id: NonBlank | None = None
    model_id: NonBlank | None = None
    extensions: JsonObject = Field(default_factory=dict)


class OperationContext(Contract):
    """用户输入中明确的共享引用；空值保持未指定，不自动恢复会话焦点。"""

    task_reference: TaskReference | None = None
    execution_reference: ExecutionReference | None = None
    scope: OperationScope | None = None


class OperationNode(Contract):
    """一个业务语义节点；无执行 ID、状态和提交副作用。"""

    operation_id: NonBlank
    action: ActionType
    target: TargetType
    task_reference: TaskReference | None = None
    execution_reference: ExecutionReference | None = None
    scope: OperationScope | None = None
    parameters: OperationParameters = Field(default_factory=OperationParameters)
    constraints: list[OperationConstraint] = Field(default_factory=list)
    input_refs: list[OperationInputReference] = Field(default_factory=list)
    output_alias: NonBlank | None = None


class OperationCondition(Contract):
    """保留用户条件及其作用节点；expression 仅为文本，禁止求值或执行。"""

    expression: NonBlank
    operation_ids: list[NonBlank] = Field(min_length=1)


class OutputRequirement(Contract):
    """用户期望的输出说明与选定操作输出，不承诺已实现渲染能力。"""

    description: NonBlank
    operation_ids: list[NonBlank] = Field(default_factory=list)


class OperationPlan(Contract):
    """承载单个或复合意图；空节点允许表达澄清、能力询问及领域外输入。

    缺失引用、约束冲突、图合法性、跨任务写入和能力适配均需后续验证，
    Pydantic 构造成功不等于可以执行。persist_mode 必须显式提供。
    """

    input_classification: InputClassification
    shared_context: OperationContext = Field(default_factory=OperationContext)
    operations: list[OperationNode] = Field(default_factory=list)
    edges: list[OperationEdge] = Field(default_factory=list)
    conditions: list[OperationCondition] = Field(default_factory=list)
    persist_mode: PersistMode
    output_requirement: OutputRequirement | None = None
    original_instruction: NonBlank


class ResolutionConfidence(StrEnum):
    """解析可信程度，不作为自动执行授权。"""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ResolutionOutcome(StrEnum):
    """未来解析结果语义；不替换现有 InteractionDecision。"""

    EXECUTABLE = "EXECUTABLE"
    READ_ONLY = "READ_ONLY"
    NEED_CLARIFICATION = "NEED_CLARIFICATION"
    KNOWN_UNSUPPORTED = "KNOWN_UNSUPPORTED"
    REJECTED = "REJECTED"


class OperationResolution(Contract):
    """解析结果的数据外壳；本 Task 没有生成或验证该裁决的 Resolver。"""

    outcome: ResolutionOutcome
    confidence: ResolutionConfidence
    plan: OperationPlan | None = None
    evidence: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    recommended_action: str | None = None
