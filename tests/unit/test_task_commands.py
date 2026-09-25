"""命令契约复用 Override，历史选择与状态投影必须来自持久事实。"""

import pytest
from pydantic import ValidationError

from cnlc_agent.application.commands import (
    FullRerunCommand,
    GetReportCommand,
    GetStatusCommand,
    ModifyInterpretationCommand,
)
from cnlc_agent.config.settings import AppSettings, PersistenceSettings
from cnlc_agent.demo.task_tools import TaskCommandRunner
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import ApplicationError
from cnlc_agent.domain.models import TaskRequest
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.state import InterpretationState


def runner(data_dir):
    return TaskCommandRunner(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None),
        PersistenceSettings(persistence="memory", _env_file=None),
    )


@pytest.mark.parametrize("changes", [
    {"sw": 0.3}, {"rw": 0.03}, {"archie_m": 2}, {"archie_n": 2},
    {"unknown_parameter": 1}, {"por": 1.1}, {"por": -0.1},
    {"sampling_interval": 0}, {"perm": -1}, {"prediction_model": "../bad"},
])
def test_modify_uses_strict_override_validation(changes):
    with pytest.raises(ValidationError):
        ModifyInterpretationCommand.model_validate({"task_id": "known", "changes": changes})


async def test_commands_reuse_planner_and_preserve_full_rerun_overrides(data_dir):
    session = runner(data_dir)
    initial = await session.run("WELL_MOCK_001")
    for changes, reused in [
        (InterpretationOverride(por=0.16, perm=0.16), list(StepId)[:3]),
        (InterpretationOverride(sampling_interval=0.1), [StepId.W01]),
        (InterpretationOverride(prediction_model="prediction-v2"), list(StepId)[:3]),
    ]:
        result = await session.execute(ModifyInterpretationCommand(
            task_id=initial.task_id, changes=changes
        ))
        execution = await session.repository.get_execution(result.execution_id)
        assert result.reused_steps == reused
        assert [e.step_id for e in execution.state_snapshot.executions] == [
            step for step in StepId if step not in reused
        ]
        assert result.effective_override.por == result.effective_override.perm == 0.16
    full = await session.execute(FullRerunCommand(task_id=initial.task_id))
    assert full.reused_steps == []
    assert full.completed_steps == list(StepId)
    assert full.effective_override == result.effective_override
    assert full.tool_run_summary["count"] == 6
    assert session.settings.model_provider == "mock"
    with pytest.raises(ApplicationError, match="变化|修改") as caught:
        await session.execute(ModifyInterpretationCommand(
            task_id=initial.task_id, changes=InterpretationOverride(por=0.16)
        ))
    assert caught.value.code == "NO_EFFECTIVE_CHANGE"
    assert len(await session.repository.list_executions(initial.task_id)) == 5


async def test_status_and_report_selectors_use_repository_and_check_binding(data_dir):
    session = runner(data_dir)
    first = await session.run("WELL_MOCK_001")
    with pytest.raises(ApplicationError) as caught:
        await session.execute(GetReportCommand(task_id=first.task_id, selector="PREVIOUS"))
    assert caught.value.code == "REPORT_NOT_FOUND"
    second = await session.execute(ModifyInterpretationCommand(
        task_id=first.task_id, changes=InterpretationOverride(por=0.16)
    ))
    for selector, execution_id in [
        ("CURRENT", second.execution_id), ("PREVIOUS", first.execution_id),
        ("LATEST_SUCCESSFUL", second.execution_id),
    ]:
        report = await session.execute(GetReportCommand(task_id=first.task_id, selector=selector))
        assert report.execution_id == execution_id
        assert report.report_markdown == await session.repository.get_execution_report(execution_id)
    explicit = await session.execute(GetReportCommand(
        task_id=first.task_id, execution_id=first.execution_id
    ))
    assert explicit.report_markdown == first.report_markdown
    other = await session.run("WELL_MOCK_001")
    with pytest.raises(ApplicationError):
        await session.execute(GetReportCommand(
            task_id=other.task_id, execution_id=first.execution_id
        ))
    status = await session.execute(GetStatusCommand(task_id=first.task_id))
    saved = await session.repository.get_execution(second.execution_id)
    assert status.status == saved.status
    assert status.execution_sequence == saved.sequence == 2
    assert status.current_execution_id == second.execution_id
    assert status.effective_override == saved.override_snapshot
    assert status.completed_steps == saved.state_snapshot.completed_steps
    assert status.reused_steps == [StepId.W01, StepId.W02, StepId.W03]
    assert status.report_ready and status.report_markdown is None
    assert status.input_version_id == saved.input_version_id
    assert status.tool_run_summary == {
        "count": 4, "last_tool": "merge_intervals", "failed_count": 0
    }
    assert not any(key in status.model_dump() for key in ("raw_data", "processed_data", "errors"))
    # 当前版本失败时，最近成功版仍指向旧成功报告。
    failed = InterpretationState(
        task=TaskRequest(task_id=first.task_id, well_id=first.well_id), status=StepStatus.FAILED
    )
    await session.repository.create_execution(failed, "RERUN")
    await session.repository.save(failed, "失败诊断报告")
    latest = await session.execute(GetReportCommand(
        task_id=first.task_id, selector="LATEST_SUCCESSFUL"
    ))
    assert latest.execution_id == second.execution_id
    actual = await session.execute(GetStatusCommand(task_id=first.task_id))
    assert actual.status == StepStatus.FAILED
