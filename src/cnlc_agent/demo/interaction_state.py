"""派生交互状态与集中策略；业务事实始终由任务仓库提供。"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, computed_field

from cnlc_agent.demo.task_context import TaskReference
from cnlc_agent.domain.execution import ExecutionStatus
from cnlc_agent.domain.models import Contract


class InteractionPhase(StrEnum):
    """交互可用性，不复制业务执行状态。"""

    NO_TASK = "NO_TASK"
    READY = "READY"
    ACTIVE = "ACTIVE"
    NEED_CLARIFICATION = "NEED_CLARIFICATION"


class PendingClarification(Contract):
    """仅下一轮可补齐的修改意图；固定目标井，禁止从聊天摘要恢复。"""

    operation: Literal["MODIFY"] = "MODIFY"
    known_value: float = Field(allow_inf_nan=False)
    missing: Literal["PARAMETER_NAME"] = "PARAMETER_NAME"
    task_reference: TaskReference
    task_id: str
    owner_token: str
    created_turn: int
    expires_at: float


class InteractionSnapshot(Contract):
    """Session 焦点与持久 Execution 的只读投影，无独立数据库实体。"""

    session_has_task: bool = False
    active_task_id: str | None = None
    active_well_id: str | None = None
    current_execution_id: str | None = None
    execution_status: ExecutionStatus | None = None
    execution_sequence: int | None = None
    current_step: str | None = None
    report_ready: bool = False
    has_previous_execution: bool = False
    has_previous_task: bool = False
    pending_clarification: PendingClarification | None = None

    # Pydantic 导出只读属性；mypy 尚不支持叠加 property 装饰器。
    @computed_field  # type: ignore[prop-decorator]
    @property
    def phase(self) -> InteractionPhase:
        """运行优先于澄清，防止旧澄清绕过活跃执行保护。"""

        if not self.session_has_task:
            return InteractionPhase.NO_TASK
        if self.execution_status in {ExecutionStatus.QUEUED, ExecutionStatus.RUNNING}:
            return InteractionPhase.ACTIVE
        if self.pending_clarification is not None:
            return InteractionPhase.NEED_CLARIFICATION
        return InteractionPhase.READY


class InteractionDecision(Contract):
    """动作的集中裁决；错误文案只由受控事实和固定文本组成。"""

    decision: Literal["ALLOW", "READ_ONLY", "CLARIFY", "REJECT"]
    error_code: str | None = None
    message: str = ""


class InteractionPolicy:
    """决定动作是否允许；不决定专业步骤、复用范围或算法。"""

    @staticmethod
    def decide(
        snapshot: InteractionSnapshot,
        action: Literal["START", "MODIFY", "FULL_RERUN", "STATUS", "GET_REPORT"],
        *,
        selector: str = "CURRENT",
        ambiguous: bool = False,
        unsupported_parameter: bool = False,
        unsupported_operation: bool = False,
        conflict: bool = False,
    ) -> InteractionDecision:
        """领域语义由同一次 ReAct 提供，执行可用性由快照确定。"""

        if conflict:
            return InteractionDecision(
                decision="CLARIFY",
                error_code="CLARIFICATION_REQUIRED",
                message="修改参数会自动重新解释并返回报告。请确认要修改参数，还是全部重跑；"
                "步骤级重算和版本比较当前尚不支持。本次未执行任何修改。",
            )
        if unsupported_parameter or unsupported_operation:
            return InteractionDecision(
                decision="REJECT",
                error_code="UNSUPPORTED_PARAMETER"
                if unsupported_parameter
                else "UNSUPPORTED_OPERATION",
                message=(
                    "当前参数契约仅支持采样间隔、孔隙度、渗透率和专业预测模型；"
                    "该参数尚不支持，本次没有产生新的执行。"
                )
                if unsupported_parameter
                else "当前尚不支持该局部重算或细粒度层段证据查询；可以查看已有报告，"
                "修改受支持参数后重跑，或明确要求全部重新跑。本次没有产生新的执行。",
            )
        if action == "START":
            return InteractionDecision(decision="ALLOW")
        if not snapshot.session_has_task:
            return InteractionDecision(
                decision="REJECT",
                error_code="TASK_NOT_FOUND",
                message="请先上传井资料或开始一次解释任务。",
            )
        if action in {"MODIFY", "FULL_RERUN"} and snapshot.phase == InteractionPhase.ACTIVE:
            return InteractionDecision(
                decision="REJECT",
                error_code="TASK_EXECUTION_ACTIVE",
                message=f"当前任务已有 Execution #{snapshot.execution_sequence} 正在排队或运行，"
                f"当前步骤 {snapshot.current_step or '尚未开始'}，请等待完成后再修改或重跑。",
            )
        if ambiguous:
            return InteractionDecision(
                decision="CLARIFY",
                error_code="CLARIFICATION_REQUIRED",
                message="请明确要修改孔隙度、渗透率还是采样间隔。",
            )
        if action == "GET_REPORT" and selector == "CURRENT" and not snapshot.report_ready:
            return InteractionDecision(
                decision="REJECT",
                error_code="REPORT_NOT_READY",
                message="当前 Execution 尚无可用报告。可明确查询上一版或最近成功版报告。",
            )
        return InteractionDecision(
            decision="READ_ONLY" if action in {"STATUS", "GET_REPORT"} else "ALLOW"
        )


def render_interaction_result(payload: dict[str, Any]) -> str:
    """将安全读模型转为用户文案，不把原始异常或聊天记忆用于解释。"""

    if payload.get("error_code") and not payload.get("command"):
        return str(payload.get("message") or "任务操作失败，请检查资料或服务配置。")
    if payload.get("report_markdown"):
        return str(payload["report_markdown"])
    sequence = payload.get("execution_sequence")
    status = payload.get("execution_status")
    current = payload.get("current_step") or "暂无运行步骤"
    completed = "、".join(payload.get("completed_steps") or []) or "无"
    detail = ""
    if status == "FAILED":
        detail = (
            f"失败步骤：{payload.get('failed_step') or current}；"
            f"错误代码：{payload.get('error_code') or 'EXECUTION_FAILED'}。"
        )
    elif status == "BLOCKED":
        missing = "、".join(
            f"{item['name']}（{item['affected_step']}）" for item in payload.get("missing_data", [])
        )
        detail = f"缺少资料：{missing or '请检查必需资料'}；当前执行已停止。"
    elif status == "REVIEW_REQUIRED":
        detail = "当前执行需要人工复核。"
    elif status == "WARNING":
        detail = "执行完成但存在 warning，请查看报告中的告警。"
    return (
        f"Execution #{sequence} 状态为 {status}；当前步骤：{current}；已完成：{completed}。{detail}"
    )
