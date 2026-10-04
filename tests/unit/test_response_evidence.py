"""Response Evidence Contract 与固定 renderer 的业务安全门控测试。"""

import pytest

from cnlc_agent.application.commands import TaskCommandResult
from cnlc_agent.demo.response_renderer import render_response_evidence
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.execution import ExecutionStatus
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.response_evidence import (
    ResponseEvidenceEnvelope,
    ResponseEvidenceKind,
    ResponseEvidenceRef,
    ResponseOperation,
    ResponseScopeEvidence,
    ResponseSourceType,
    ResponseStatus,
    response_envelope_from_task_result,
)


def _task_result(
    *,
    command: str = "START",
    execution_status: ExecutionStatus = ExecutionStatus.SUCCESS,
    report: str | None = None,
    source_type: ResponseSourceType = ResponseSourceType.REAL,
) -> TaskCommandResult:
    """构造与仓库投影等价的最小事实样本。"""

    refs = [
        ResponseEvidenceRef(kind=ResponseEvidenceKind.TASK, reference_id="task-13"),
        ResponseEvidenceRef(
            kind=ResponseEvidenceKind.EXECUTION,
            reference_id="exec-13",
            source_type=source_type,
        ),
    ]
    if report is not None:
        refs.append(
            ResponseEvidenceRef(
                kind=ResponseEvidenceKind.REPORT,
                reference_id="exec-13",
                source_type=source_type,
            )
        )
    return TaskCommandResult(
        command=command,
        task_id="task-13",
        well_id="WELL-13",
        well_name="Mock Well",
        execution_id="exec-13",
        current_execution_id="exec-13",
        execution_sequence=3,
        execution_status=execution_status,
        workflow_status=StepStatus.SUCCESS,
        current_step=None,
        completed_steps=[],
        reused_steps=[],
        effective_override=InterpretationOverride(),
        input_version_id="input-v3",
        start_step=None,
        source_execution_id=None,
        planning_reason="INITIAL",
        started_at=None,
        finished_at=None,
        error_code=None,
        report_ready=report is not None,
        tool_run_summary={"count": 1},
        summary="第 3 版执行状态：SUCCESS；Workflow 状态：SUCCESS。",
        report_markdown=report,
        source_type=source_type,
        evidence_refs=tuple(refs),
    )


def test_case_1_missing_application_result_cannot_render_success():
    with pytest.raises(ValueError, match="禁止构造 SUCCESS"):
        ResponseEvidenceEnvelope.without_result(status=ResponseStatus.SUCCESS)
    assert "没有查询或修改" in render_response_evidence(None)
    assert "已完成" not in render_response_evidence(None)

    with pytest.raises(ValueError, match="matching Task and Execution"):
        ResponseEvidenceEnvelope(
            operation=ResponseOperation.START,
            status=ResponseStatus.SUCCESS,
        )


def test_case_2_need_clarification_only_asks_for_more_information():
    envelope = ResponseEvidenceEnvelope.without_result(
        status=ResponseStatus.NEED_CLARIFICATION,
        clarification="请明确要修改的层段范围。",
    )
    text = render_response_evidence(envelope)
    assert text == "请明确要修改的层段范围。"
    assert "已修改" not in text
    assert "已完成" not in text


def test_case_3_unsupported_is_not_rendered_as_success():
    envelope = ResponseEvidenceEnvelope.without_result(
        status=ResponseStatus.UNSUPPORTED,
        result_summary="当前不支持局部重算，未执行。",
    )
    text = render_response_evidence(envelope)
    assert "不支持" in text
    assert "成功" not in text


def test_case_4_rejected_request_does_not_claim_parameter_change():
    envelope = ResponseEvidenceEnvelope.without_result(
        operation=ResponseOperation.MODIFY,
        status=ResponseStatus.REJECTED,
        result_summary="请求已拒绝，未执行。",
    )
    text = render_response_evidence(envelope)
    assert "未执行" in text
    assert "修改并重新解释已完成" not in text


def test_case_5_running_execution_cannot_claim_interpretation_complete():
    result = _task_result(execution_status=ExecutionStatus.RUNNING)
    envelope = response_envelope_from_task_result(result)
    assert envelope.status == ResponseStatus.RUNNING
    assert "正在进行" in render_response_evidence(envelope)
    assert "解释执行已完成" not in render_response_evidence(envelope)


def test_case_6_success_response_uses_exact_task_execution_scope_and_version():
    result = _task_result()
    scope = ResponseScopeEvidence(
        kind="INTERVAL",
        references=("exec-13:interval-2",),
        description="第 2 层",
    )
    envelope = response_envelope_from_task_result(
        result, operation=ResponseOperation.MODIFY, scope=scope
    )
    text = render_response_evidence(envelope)
    assert envelope.task_id == "task-13"
    assert envelope.execution_id == "exec-13"
    assert envelope.scope == scope
    assert envelope.input_version_id == "input-v3"
    assert "Task：task-13" in text
    assert "Execution：exec-13" in text
    assert "第 2 层" in text
    assert "第 3 版" in text


@pytest.mark.parametrize("source", [ResponseSourceType.FIXTURE, ResponseSourceType.MOCK])
def test_case_7_fixture_and_mock_are_labeled_non_real(source):
    envelope = response_envelope_from_task_result(_task_result(source_type=source))
    text = render_response_evidence(envelope)
    assert "非真实业务结果" in text


def test_case_8_modify_without_formal_execution_result_cannot_succeed():
    envelope = ResponseEvidenceEnvelope.without_result(
        operation=ResponseOperation.MODIFY,
        status=ResponseStatus.FAILED,
        error_code="NO_EXECUTION_RESULT",
    )
    text = render_response_evidence(envelope)
    assert "解释执行失败" in text
    assert "修改并重新解释已完成" not in text


def test_case_9_report_read_and_report_generation_are_distinct_operations():
    result = _task_result(command="GET_REPORT", report="# Exact persisted report")
    read_envelope = response_envelope_from_task_result(
        result, operation=ResponseOperation.REPORT_READ
    )
    generated_envelope = response_envelope_from_task_result(
        result, operation=ResponseOperation.REPORT_GENERATION
    )
    assert render_response_evidence(read_envelope).endswith("# Exact persisted report")
    assert render_response_evidence(generated_envelope).endswith("# Exact persisted report")
    assert read_envelope.operation != generated_envelope.operation


def test_case_10_stage_confirmation_does_not_claim_next_stage_complete():
    queued = _task_result(command="CONFIRM", execution_status=ExecutionStatus.QUEUED).model_copy(
        update={"confirmed_stage": "PREPROCESS"}
    )
    envelope = response_envelope_from_task_result(queued, operation=ResponseOperation.STAGE_CONFIRM)
    text = render_response_evidence(envelope)
    assert "已确认数据预处理阶段" in text
    assert "不代表下一阶段已经完成" in text


def test_stage_report_confirmation_distinguishes_confirmed_and_generated_report():
    result = _task_result(command="CONFIRM", report="# Confirmed report").model_copy(
        update={"confirmed_stage": "REPORT"}
    )
    envelope = response_envelope_from_task_result(result, operation=ResponseOperation.STAGE_CONFIRM)
    text = render_response_evidence(envelope)
    assert "已确认报告生成阶段；报告生成已完成" in text
    assert "# Confirmed report" in text
