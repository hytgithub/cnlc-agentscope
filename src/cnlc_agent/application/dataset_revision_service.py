"""DatasetRevision 应用服务：解析可信基线并原子追加版本事实。"""

from cnlc_agent.application.ports import TaskRepository
from cnlc_agent.domain.dataset_revision import (
    DatasetChangeSet,
    DatasetCurveChange,
    DatasetPatchRequest,
    DatasetRevision,
    DatasetSampleChange,
    apply_curve_changes,
    change_set_digest,
    child_lineage,
    root_lineage,
)
from cnlc_agent.domain.errors import DataError
from cnlc_agent.domain.inputs import InputPayloadKind
from cnlc_agent.domain.models import RawData


class DatasetRevisionService:
    """不读取 ActiveContext，只验证显式 task_id、revision_id 和 Repository 归属。"""

    def __init__(self, repository: TaskRepository) -> None:
        self.repository = repository

    async def create_root_revision(
        self, task_id: str, input_version_id: str, *, source_execution_id: str | None = None
    ) -> DatasetRevision:
        """创建只引用 InputVersion 的根版本，不复制 payload。"""

        input_version = await self.repository.get_input_version(input_version_id)
        if input_version is None or input_version.task_id != task_id:
            raise DataError("INPUT_VERSION_TASK_MISMATCH", "输入版本不属于当前任务")
        task = await self.repository.get_task(task_id)
        if task is None:
            raise DataError("TASK_NOT_FOUND", "任务不存在")
        if task.well_id != input_version.well_id:
            raise DataError("TASK_WELL_MISMATCH", "任务与输入井不一致")
        revision = DatasetRevision(
            task_id=task_id,
            well_id=input_version.well_id,
            sequence=1,
            root_input_version_id=input_version_id,
            lineage_sha256=root_lineage(input_version.content_sha256),
            created_from_execution_id=source_execution_id,
        )
        return await self.repository.create_root_dataset_revision(revision)

    async def materialize(self, task_id: str, revision_id: str) -> RawData:
        """沿父链找到根输入，再按 Root→C1→…→Cn 顺序应用增量。"""

        revision = await self.repository.get_dataset_revision(revision_id)
        if revision is None or revision.task_id != task_id:
            raise DataError("DATASET_REVISION_TASK_MISMATCH", "数据集版本不属于当前任务")
        chain: list[DatasetRevision] = []
        visited: set[str] = set()
        current = revision
        while True:
            if current.dataset_revision_id in visited:
                raise DataError("INVALID_DATASET_REVISION_CHAIN", "数据集版本链存在循环")
            if len(chain) >= 256:
                raise DataError("DATASET_REVISION_CHAIN_TOO_DEEP", "数据集版本链超过最大深度")
            visited.add(current.dataset_revision_id)
            chain.append(current)
            if current.parent_revision_id is None:
                break
            parent = await self.repository.get_dataset_revision(current.parent_revision_id)
            if parent is None or parent.task_id != task_id:
                raise DataError("INVALID_DATASET_REVISION_CHAIN", "父版本不存在或跨任务")
            if (
                parent.well_id != revision.well_id
                or parent.root_input_version_id != revision.root_input_version_id
            ):
                raise DataError("INVALID_DATASET_REVISION_CHAIN", "版本链根输入不一致")
            current = parent
        root_input = await self.repository.get_input_version(chain[-1].root_input_version_id)
        if (
            root_input is None
            or root_input.task_id != task_id
            or root_input.well_id != revision.well_id
        ):
            raise DataError("INPUT_VERSION_TASK_MISMATCH", "根输入不属于当前任务或井")
        root = chain[-1]
        if (
            root.parent_revision_id is not None
            or root.change_set_id is not None
            or root.lineage_sha256 != root_lineage(root_input.content_sha256)
        ):
            raise DataError("INVALID_DATASET_REVISION_CHAIN", "根版本 lineage 无效")
        if root_input.payload_kind == InputPayloadKind.GDSX_ARTIFACT:
            raise DataError(
                "ARTIFACT_DATASET_MATERIALIZATION_UNSUPPORTED",
                "当前不支持把 Artifact-backed GDSX 全量物化为 RawData",
            )
        assert root_input.payload is not None
        raw_data = root_input.payload.raw_data.model_copy(deep=True)
        for item in reversed(chain[:-1]):
            if item.change_set_id is None:
                raise DataError("INVALID_DATASET_REVISION_CHAIN", "子版本缺少 ChangeSet")
            change_set = await self.repository.get_dataset_change_set(item.change_set_id)
            if (
                change_set is None
                or change_set.task_id != task_id
                or change_set.well_id != revision.well_id
                or change_set.base_revision_id != item.parent_revision_id
            ):
                raise DataError("INVALID_DATASET_REVISION_CHAIN", "ChangeSet 与父版本不一致")
            parent = next(
                candidate
                for candidate in chain
                if candidate.dataset_revision_id == item.parent_revision_id
            )
            if change_set.content_sha256 != change_set_digest(
                change_set.curve_changes
            ) or item.lineage_sha256 != child_lineage(
                parent.lineage_sha256, change_set.content_sha256
            ):
                raise DataError("INVALID_DATASET_REVISION_CHAIN", "ChangeSet 摘要或 lineage 无效")
            raw_data = apply_curve_changes(raw_data, change_set.curve_changes)
        return raw_data

    async def apply_patch(
        self,
        task_id: str,
        base_revision_id: str,
        patch_request: DatasetPatchRequest,
        *,
        actor: str,
        source_execution_id: str | None = None,
        reason: str | None = None,
        original_instruction: str | None = None,
    ) -> tuple[DatasetRevision, DatasetChangeSet]:
        """显式基线创建 Child Revision；绝不隐式选择最新版本。"""

        base = await self.repository.get_dataset_revision(base_revision_id)
        if base is None or base.task_id != task_id:
            raise DataError("DATASET_REVISION_TASK_MISMATCH", "基线版本不属于当前任务")
        task = await self.repository.get_task(task_id)
        if task is None:
            raise DataError("TASK_NOT_FOUND", "任务不存在")
        if task.well_id != base.well_id:
            raise DataError("TASK_WELL_MISMATCH", "任务与基线井不一致")
        raw_data = await self.materialize(task_id, base_revision_id)
        changes = self._resolve_changes(raw_data, patch_request)
        digest = change_set_digest(changes)
        change_set = DatasetChangeSet(
            task_id=task_id,
            well_id=base.well_id,
            base_revision_id=base_revision_id,
            curve_changes=changes,
            content_sha256=digest,
            created_by=actor,
            source_execution_id=source_execution_id,
            reason=reason,
            original_instruction=original_instruction,
        )
        revision = DatasetRevision(
            task_id=task_id,
            well_id=base.well_id,
            # 正式序号由 Repository 在 Task 锁/事务内覆盖分配；此值只是入参占位。
            sequence=1,
            root_input_version_id=base.root_input_version_id,
            parent_revision_id=base_revision_id,
            change_set_id=change_set.change_set_id,
            lineage_sha256=child_lineage(base.lineage_sha256, digest),
            created_from_execution_id=source_execution_id,
        )
        return await self.repository.create_revision_with_change_set(revision, change_set)

    @staticmethod
    def _resolve_changes(
        raw_data: RawData, request: DatasetPatchRequest
    ) -> list[DatasetCurveChange]:
        """按真实 MD 深度轴生成可信 sample_index、before_value 和 after_value。"""

        changes: list[DatasetCurveChange] = []
        seen_curves: set[str] = set()
        for curve_request in request.curves:
            if curve_request.curve_code in seen_curves:
                raise DataError("DUPLICATE_PATCH_CURVE", "同一 ChangeSet 不能重复声明曲线")
            seen_curves.add(curve_request.curve_code)
            curve = raw_data.curves.get(curve_request.curve_code)
            if curve is None:
                raise DataError("DATASET_CURVE_NOT_FOUND", "基线中不存在指定曲线")
            if curve.unit != curve_request.unit:
                raise DataError("DATASET_CURVE_UNIT_MISMATCH", "曲线单位与基线不一致")
            samples: list[DatasetSampleChange] = []
            indexes: set[int] = set()
            for requested_sample in curve_request.samples:
                try:
                    index = raw_data.depths.index(requested_sample.depth_m)
                except ValueError:
                    raise DataError("PATCH_DEPTH_NOT_FOUND", "指定深度不在基线采样轴中") from None
                if index in indexes:
                    raise DataError("DUPLICATE_PATCH_SAMPLE", "同一曲线采样点重复修改")
                indexes.add(index)
                before = curve.values[index]
                if before == requested_sample.new_value:
                    continue
                samples.append(
                    DatasetSampleChange(
                        sample_index=index,
                        depth_m=raw_data.depths[index],
                        before_value=before,
                        after_value=requested_sample.new_value,
                    )
                )
            if samples:
                changes.append(
                    DatasetCurveChange(
                        curve_code=curve_request.curve_code,
                        unit=curve_request.unit,
                        samples=samples,
                    )
                )
        if not changes:
            raise DataError("NO_EFFECTIVE_DATASET_CHANGE", "修改没有产生有效数据变化")
        return changes
