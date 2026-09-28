"""DatasetRevision / ChangeSet 的不可变 lineage 与稀疏修改测试。"""

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from cnlc_agent.application.dataset_revision_service import DatasetRevisionService
from cnlc_agent.domain.dataset_revision import (
    DatasetChangeSet,
    DatasetCurveChange,
    DatasetCurvePatchRequest,
    DatasetPatchRequest,
    DatasetPatchSampleRequest,
    DatasetRevision,
    DatasetSampleChange,
    change_set_digest,
    child_lineage,
)
from cnlc_agent.domain.errors import DataError, InfrastructureError
from cnlc_agent.domain.models import MockFixture, TaskRequest
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.infrastructure.mock import InMemoryTaskRepository


def load_fixture() -> MockFixture:
    """读取现有正式 Fixture，避免在测试里补造曲线业务规则。"""

    path = Path(__file__).resolve().parents[2] / "mock_data/WELL_MOCK_001.json"
    return MockFixture.model_validate_json(path.read_text())


async def prepare(
    repository: InMemoryTaskRepository | None = None,
    fixture: MockFixture | None = None,
) -> tuple[
    InMemoryTaskRepository,
    DatasetRevisionService,
    TaskRequest,
    MockFixture,
    DatasetRevision,
]:
    """建立 Task、InputVersion 和 Root Revision。"""

    repository = repository or InMemoryTaskRepository()
    fixture = fixture or load_fixture()
    request = TaskRequest(well_id=fixture.well.well_id)
    await repository.create_task(InterpretationState(task=request))
    input_version = await repository.create_input_version(request.task_id, fixture)
    service = DatasetRevisionService(repository)
    root = await service.create_root_revision(request.task_id, input_version.input_version_id)
    return repository, service, request, fixture, root


def patch(curve: str, unit: str, *samples: tuple[float, float | None]) -> DatasetPatchRequest:
    """构造只含深度和目标值的外部请求。"""

    return DatasetPatchRequest(
        curves=[
            DatasetCurvePatchRequest(
                curve_code=curve,
                unit=unit,
                samples=[
                    DatasetPatchSampleRequest(depth_m=depth, new_value=value)
                    for depth, value in samples
                ],
            )
        ]
    )


async def test_root_revision_is_metadata_only_and_preserves_input() -> None:
    repository, service, request, fixture, root = await prepare()
    input_version = (await repository.list_input_versions(request.task_id))[0]
    original = input_version.model_dump_json()

    assert root.sequence == 1
    assert root.root_input_version_id == input_version.input_version_id
    assert root.parent_revision_id is None
    assert root.change_set_id is None
    assert not ({"payload", "raw_data", "depths", "curves"} & set(DatasetRevision.model_fields))
    materialized = await service.materialize(request.task_id, root.dataset_revision_id)
    assert materialized == fixture.raw_data
    materialized.curves["GR"].values[0] = 999.0
    stored_input = await repository.get_input_version(input_version.input_version_id)
    assert stored_input is not None and stored_input.model_dump_json() == original


async def test_sparse_patch_chain_multicurve_and_explicit_branch() -> None:
    repository, service, request, fixture, root = await prepare()
    original_input = (await repository.list_input_versions(request.task_id))[0].model_dump_json()
    depths = fixture.raw_data.depths

    r2, c1 = await service.apply_patch(
        request.task_id,
        root.dataset_revision_id,
        patch("GR", "API", (depths[0], 82.0), (depths[1], 84.0)),
        actor="tester",
        reason="修正 GR",
    )
    r3, c2 = await service.apply_patch(
        request.task_id,
        r2.dataset_revision_id,
        DatasetPatchRequest(
            curves=[
                DatasetCurvePatchRequest(
                    curve_code="GR",
                    unit="API",
                    samples=[DatasetPatchSampleRequest(depth_m=depths[2], new_value=None)],
                ),
                DatasetCurvePatchRequest(
                    curve_code="RT",
                    unit="ohm.m",
                    samples=[DatasetPatchSampleRequest(depth_m=depths[1], new_value=30.0)],
                ),
            ]
        ),
        actor="tester",
    )
    branch, branch_change = await service.apply_patch(
        request.task_id,
        root.dataset_revision_id,
        patch("RT", "ohm.m", (depths[0], 25.0)),
        actor="tester",
    )

    assert [item.sequence for item in await repository.list_dataset_revisions(request.task_id)] == [
        1,
        2,
        3,
        4,
    ]
    assert r2.parent_revision_id == root.dataset_revision_id
    assert r2.change_set_id == c1.change_set_id
    assert r3.parent_revision_id == r2.dataset_revision_id
    assert branch.parent_revision_id == root.dataset_revision_id
    assert branch_change.base_revision_id == root.dataset_revision_id
    assert len(c1.curve_changes) == 1 and len(c1.curve_changes[0].samples) == 2
    assert {change.curve_code for change in c2.curve_changes} == {"GR", "RT"}
    assert not ({"payload", "raw_data", "depths", "curves"} & set(DatasetRevision.model_fields))
    assert "depths" not in c2.model_dump(mode="json")

    r1_data = await service.materialize(request.task_id, root.dataset_revision_id)
    r2_data = await service.materialize(request.task_id, r2.dataset_revision_id)
    r3_data = await service.materialize(request.task_id, r3.dataset_revision_id)
    branch_data = await service.materialize(request.task_id, branch.dataset_revision_id)
    assert r1_data == fixture.raw_data
    assert r2_data.curves["GR"].values[:3] == [82.0, 84.0, 46.0]
    assert r3_data.curves["GR"].values[:3] == [82.0, 84.0, None]
    assert r3_data.curves["RT"].values[:3] == [20.0, 30.0, 21.0]
    assert branch_data.curves["RT"].values[:3] == [25.0, 22.0, 21.0]
    stored_input = (await repository.list_input_versions(request.task_id))[0]
    assert stored_input.model_dump_json() == original_input


@pytest.mark.parametrize(
    ("patch_request", "code"),
    [
        (patch("GR", "API", (2000.0, 45.0)), "NO_EFFECTIVE_DATASET_CHANGE"),
        (patch("UNKNOWN", "API", (2000.0, 1.0)), "DATASET_CURVE_NOT_FOUND"),
        (patch("GR", "API", (2000.25, 1.0)), "PATCH_DEPTH_NOT_FOUND"),
        (patch("GR", "mV", (2000.0, 1.0)), "DATASET_CURVE_UNIT_MISMATCH"),
    ],
)
async def test_rejected_patch_is_atomic(
    patch_request: DatasetPatchRequest, code: str
) -> None:
    repository, service, task, _, root = await prepare()

    with pytest.raises(DataError) as caught:
        await service.apply_patch(
            task.task_id, root.dataset_revision_id, patch_request, actor="tester"
        )
    assert caught.value.code == code
    assert len(await repository.list_dataset_revisions(task.task_id)) == 1
    assert await repository.list_dataset_change_sets(task.task_id) == []


async def test_cross_task_and_cross_well_rejected_without_active_context() -> None:
    repository, service_a, task_a, fixture_a, root_a = await prepare()
    fixture_b = fixture_a.model_copy(deep=True)
    fixture_b.well.well_id = "WELL_B"
    _, service_b, task_b, _, root_b = await prepare(repository, fixture_b)

    with pytest.raises(DataError) as caught:
        await service_b.apply_patch(
            task_b.task_id,
            root_a.dataset_revision_id,
            patch("GR", "API", (2000.0, 81.0)),
            actor="tester",
        )
    assert caught.value.code == "DATASET_REVISION_TASK_MISMATCH"

    a2, _ = await service_a.apply_patch(
        task_a.task_id,
        root_a.dataset_revision_id,
        patch("GR", "API", (2000.0, 81.0)),
        actor="tester",
    )
    assert (await service_a.materialize(task_a.task_id, a2.dataset_revision_id)).curves[
        "GR"
    ].values[0] == 81.0
    assert (await service_b.materialize(task_b.task_id, root_b.dataset_revision_id)).curves[
        "GR"
    ].values[0] == 45.0


async def test_repository_rejects_invalid_digest_before_atomic_write() -> None:
    repository, _, task, _, root = await prepare()
    sample = DatasetSampleChange(
        sample_index=0, depth_m=2000.0, before_value=45.0, after_value=80.0
    )
    curve_change = DatasetCurveChange(curve_code="GR", unit="API", samples=[sample])
    valid_digest = change_set_digest([curve_change])
    change_set = DatasetChangeSet(
        task_id=task.task_id,
        well_id=task.well_id,
        base_revision_id=root.dataset_revision_id,
        curve_changes=[curve_change],
        content_sha256="0" * 64,
        created_by="tester",
    )
    revision = DatasetRevision(
        task_id=task.task_id,
        well_id=task.well_id,
        sequence=2,
        root_input_version_id=root.root_input_version_id,
        parent_revision_id=root.dataset_revision_id,
        change_set_id=change_set.change_set_id,
        lineage_sha256=child_lineage(root.lineage_sha256, valid_digest),
    )

    with pytest.raises(InfrastructureError) as caught:
        await repository.create_revision_with_change_set(revision, change_set)
    assert caught.value.code == "INVALID_CHANGE_SET_DIGEST"
    assert len(await repository.list_dataset_revisions(task.task_id)) == 1
    assert await repository.list_dataset_change_sets(task.task_id) == []


async def test_sequence_allocation_is_serialized_and_change_set_is_single_use() -> None:
    repository, service, task, _, root = await prepare()

    results = await asyncio.gather(
        *(
            service.apply_patch(
                task.task_id,
                root.dataset_revision_id,
                patch("GR", "API", (2000.0, value)),
                actor="tester",
            )
            for value in (70.0, 71.0, 72.0, 73.0)
        )
    )
    assert [item.sequence for item in await repository.list_dataset_revisions(task.task_id)] == [
        1,
        2,
        3,
        4,
        5,
    ]

    first_revision, first_change_set = results[0]
    duplicate_revision = DatasetRevision.model_validate(
        {
            **first_revision.model_dump(mode="python"),
            "dataset_revision_id": "duplicate-change-set-revision",
        }
    )
    with pytest.raises(InfrastructureError) as caught:
        await repository.create_revision_with_change_set(
            duplicate_revision, first_change_set
        )
    assert caught.value.code == "DATASET_CHANGE_SET_ALREADY_USED"
    assert len(await repository.list_dataset_revisions(task.task_id)) == 5


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_sample_change_rejects_non_finite_values(invalid: float) -> None:
    for field in ("before_value", "after_value"):
        values = {
            "sample_index": 0,
            "depth_m": 2000.0,
            "before_value": 45.0,
            "after_value": 46.0,
            field: invalid,
        }
        with pytest.raises(ValidationError):
            DatasetSampleChange.model_validate(values)


def test_change_set_digest_uses_resolved_facts_and_ignores_list_order() -> None:
    first = DatasetCurveChange(
        curve_code="GR",
        unit="API",
        samples=[
            DatasetSampleChange(
                sample_index=1,
                depth_m=2000.5,
                before_value=48.0,
                after_value=84.0,
            ),
            DatasetSampleChange(
                sample_index=0,
                depth_m=2000.0,
                before_value=45.0,
                after_value=82.0,
            ),
        ],
    )
    second = DatasetCurveChange(
        curve_code="RT",
        unit="ohm.m",
        samples=[
            DatasetSampleChange(
                sample_index=0,
                depth_m=2000.0,
                before_value=20.0,
                after_value=None,
            )
        ],
    )
    reordered_first = DatasetCurveChange(
        curve_code=first.curve_code,
        unit=first.unit,
        samples=list(reversed(first.samples)),
    )

    assert change_set_digest([first, second]) == change_set_digest(
        [second, reordered_first]
    )
    changed_after = first.model_copy(deep=True)
    changed_after.samples[0].after_value = 85.0
    assert change_set_digest([first, second]) != change_set_digest(
        [changed_after, second]
    )


async def test_materializer_rejects_cycle_and_excessive_lineage() -> None:
    """即使持久层事实损坏，Resolver 也不会无限追踪 parent。"""

    repository, service, task, _, root = await prepare()
    repository._dataset_revisions[root.dataset_revision_id] = DatasetRevision.model_validate(
        {
            **root.model_dump(mode="python"),
            "parent_revision_id": root.dataset_revision_id,
            "change_set_id": "corrupt-cycle-change",
        }
    )
    with pytest.raises(DataError) as cycle_error:
        await service.materialize(task.task_id, root.dataset_revision_id)
    assert cycle_error.value.code == "INVALID_DATASET_REVISION_CHAIN"

    repository, service, task, _, root = await prepare()
    parent = root
    for sequence in range(2, 258):
        child = DatasetRevision(
            task_id=task.task_id,
            well_id=task.well_id,
            sequence=sequence,
            root_input_version_id=root.root_input_version_id,
            parent_revision_id=parent.dataset_revision_id,
            change_set_id=f"corrupt-change-{sequence}",
            lineage_sha256="0" * 64,
        )
        repository._dataset_revisions[child.dataset_revision_id] = child
        parent = child
    with pytest.raises(DataError) as depth_error:
        await service.materialize(task.task_id, parent.dataset_revision_id)
    assert depth_error.value.code == "DATASET_REVISION_CHAIN_TOO_DEEP"
