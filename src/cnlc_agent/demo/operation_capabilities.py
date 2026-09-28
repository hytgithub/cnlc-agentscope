"""声明当前开放的操作能力；与语义 Schema、实际 Tool 实现分别演进。

Catalog 不导入或调用 Handler，不是权限、引用解析或计划校验器。
"""

from collections.abc import Iterable
from enum import StrEnum

from pydantic import ConfigDict, Field

from cnlc_agent.demo.operation_models import ActionType, NonBlank, OperationScopeKind
from cnlc_agent.domain.models import Contract


class OperationCapabilityStatus(StrEnum):
    """区分业务开关、业务待确认与代码缺口；只有已启用具有执行资格。"""

    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    UNVERIFIED = "UNVERIFIED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class OperationCapability(Contract):
    """只读能力声明；handler_name 是实现线索，不能据此动态导入或执行。"""

    model_config = ConfigDict(frozen=True)

    action: ActionType
    status: OperationCapabilityStatus = OperationCapabilityStatus.UNVERIFIED
    allowed_scopes: tuple[OperationScopeKind, ...] = ()
    description: NonBlank
    handler_name: NonBlank | None = None
    business_note: NonBlank | None = None
    supported_parameters: tuple[NonBlank, ...] = Field(default_factory=tuple)

    @property
    def is_executable(self) -> bool:
        """只判断能力开关资格；具体请求仍需校验范围、参数、归属和当前状态。"""

        return self.status == OperationCapabilityStatus.ENABLED


def default_capabilities() -> tuple[OperationCapability, ...]:
    """按稳定基线真实任务工具声明能力；未确认需求不冒充已实现或领域外。"""

    enabled = OperationCapabilityStatus.ENABLED
    unavailable = OperationCapabilityStatus.NOT_IMPLEMENTED
    unverified = OperationCapabilityStatus.UNVERIFIED
    whole_well = (OperationScopeKind.WHOLE_WELL,)
    capabilities = [
        OperationCapability(
            action=ActionType.FULL_INTERPRET,
            status=enabled,
            allowed_scopes=whole_well,
            description="使用已校验资料或 Fixture 开始整井首次解释。",
            handler_name="run_well_interpretation",
            business_note="对应现有 START；已有任务全量重跑仍走原 FULL_RERUN，不能混同首次创建。",
        ),
        OperationCapability(
            action=ActionType.FULL_RERUN,
            status=enabled,
            allowed_scopes=whole_well,
            description="对已有任务全量重跑并继承当前有效参数。",
            handler_name="rerun_well_interpretation",
            business_note="已有授权 Task 的全量重跑，与首次 FULL_INTERPRET 不同；"
            "不得用于局部 REINTERPRET。",
        ),
        OperationCapability(
            action=ActionType.STATUS,
            status=enabled,
            allowed_scopes=whole_well,
            description="查询任务当前持久执行状态。",
            handler_name="get_interpretation_status",
            business_note="仅现有当前任务状态投影，不承诺任意历史版本状态选择。",
        ),
        OperationCapability(
            action=ActionType.REPORT,
            status=enabled,
            allowed_scopes=whole_well,
            description="读取已有任务当前或受控历史版本报告。",
            handler_name="get_interpretation_report",
            business_note="仅已有报告读取；独立生成报告请求尚未映射，不能据此开放写操作。",
        ),
        OperationCapability(
            action=ActionType.MODIFY_PARAMETER,
            status=enabled,
            allowed_scopes=whole_well,
            description="按现有 InterpretationOverride 修改参数并由应用层规划重跑。",
            handler_name="modify_well_interpretation",
            supported_parameters=("por", "perm", "sampling_interval", "prediction_model"),
            business_note="仅现有参数的绝对设置；数值经原契约校验。预测模型用标识；"
            "不开放增量、比例变化、Rw、Archie、截止值、直接改最终结果或局部重算。",
        ),
    ]
    # 已有文档明确规划或已有底层读取，但尚无完整的独立 Operation 处理能力。
    planned = {
        ActionType.QUERY: "通用结果查询尚未开放；已有状态和报告查询各用自身动作。",
        ActionType.EXPLAIN: "受控原因与细层段证据查询尚未开放。",
        ActionType.RECALCULATE: "尚无任意目标或局部重算；不得映射成现有全量重跑。",
        ActionType.REINTERPRET: "整井重跑已有旧入口，但新语义包含目标和范围，尚无完整适配。",
        ActionType.SWITCH_MODEL: "prediction_model 仅通过现有参数修改生效，独立切模动作尚未适配。",
        ActionType.VALIDATE: "W09 已在主流程内执行，尚无独立验证操作入口。",
        ActionType.HISTORY: "已有 Read API 历史列表，尚无 Operation 历史查询处理入口。",
        ActionType.PAUSE_EXECUTION: "总体架构明确为后续能力，当前 Worker 不提供暂停。",
        ActionType.RESUME_EXECUTION: "总体架构明确为后续能力，当前 Worker 不提供恢复。",
        ActionType.RETRY_EXECUTION: "失败可由用户明确全量重跑，但尚无独立受控重试语义。",
        ActionType.NEW_WELL: "现有附件路由已创建新 Task；独立新井资料操作尚未适配。",
    }
    # 这些操作的专业规则、使用需求或生命周期尚未由业务确认。
    pending = {
        ActionType.MODIFY_RESULT: "人工修改派生结果的规则和审计边界待确认。",
        ActionType.SWITCH_METHOD: "方法清单与可切换条件待业务确认。",
        ActionType.COMPARE: "比较对象、指标和业务验收口径待确认，尚无 Compare Engine。",
        ActionType.SEGMENT_EDIT: "拆层、合层和层界编辑规则待确认。",
        ActionType.OVERRIDE: "人工最终解释覆盖是否允许及其审计规则待确认。",
        ActionType.RESTORE_VERSION: "历史恢复与后续工作基线的业务语义待确认。",
        ActionType.SCENARIO: "试算隔离与结果生命周期待业务确认。",
        ActionType.COMMIT_SCENARIO: "试算转正式版本的采用规则待业务确认。",
        ActionType.CANCEL_EXECUTION: "取消对专业外部调用的影响和业务终态待确认。",
        ActionType.ACCEPT_RESULT: "人工接受结果的流程和权限边界待确认。",
        ActionType.REJECT_RESULT: "人工拒绝结果后的流程和留痕规则待确认。",
        ActionType.MARK_FINAL: "最终版本标记与审批规则待确认。",
        ActionType.MARK_REVIEW: "人工标记复核与既有 Workflow 复核状态的关系待确认。",
        ActionType.REPLACE_INPUT: "同 Task 替换输入资料的业务规则待确认；当前重传会创建新 Task。",
        ActionType.ADD_EVIDENCE: "追加证据的输入契约与影响范围待确认，未接入 Evidence Pipeline。",
    }
    for status, entries in ((unavailable, planned), (unverified, pending)):
        capabilities.extend(
            OperationCapability(action=action, status=status, description=note, business_note=note)
            for action, note in entries.items()
        )
    return tuple(capabilities)


class OperationCatalog:
    """简单动作查表；每个实例独立保存配置，切换业务开关无需改 Operation Schema。"""

    def __init__(self, capabilities: Iterable[OperationCapability] | None = None) -> None:
        entries = default_capabilities() if capabilities is None else tuple(capabilities)
        self._capabilities: dict[ActionType, OperationCapability] = {}
        for entry in entries:
            if entry.action in self._capabilities:
                raise ValueError(f"duplicate capability: {entry.action}")
            self._capabilities[entry.action] = entry

    def get(self, action: ActionType) -> OperationCapability:
        """查询已声明的动作；未知动作或缺少配置明确报错，不隐式启用。"""

        return self._capabilities[ActionType(action)]

    def is_executable(self, action: ActionType) -> bool:
        """缺少声明时安全返回 False；已声明动作只按状态判断资格。"""

        capability = self._capabilities.get(ActionType(action))
        return capability is not None and capability.is_executable

    def list_capabilities(self) -> tuple[OperationCapability, ...]:
        """以声明顺序返回不可变快照，供能力说明和配置审查使用。"""

        return tuple(self._capabilities.values())

    def with_status(
        self, action: ActionType, status: OperationCapabilityStatus
    ) -> "OperationCatalog":
        """返回调整开关的新 Catalog；不改 Schema、既有实例或运行中的工具注册。"""

        current = self.get(action)
        changed = OperationCapability.model_validate({**current.model_dump(), "status": status})
        return OperationCatalog(
            changed if item.action == current.action else item
            for item in self._capabilities.values()
        )
