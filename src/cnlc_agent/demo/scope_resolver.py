"""只读解析指定 Execution 内的层段与深度；不转换基准、不跨版本匹配、不执行筛选。"""

from enum import StrEnum
from math import isfinite
from urllib.parse import quote

from pydantic import ConfigDict, Field, JsonValue, TypeAdapter

from cnlc_agent.application.ports import TaskRepository
from cnlc_agent.demo.operation_models import (
    DepthPointScope,
    DepthRangeScope,
    DepthReference,
    FilterSetScope,
    IntervalScope,
    MultiIntervalScope,
    OperationScope,
    OperationScopeKind,
)
from cnlc_agent.demo.operation_parser import (
    IntervalOrdinalReference,
    MultiIntervalOrdinalReference,
    PartialScope,
)
from cnlc_agent.demo.reference_resolver import OperationReferenceResolver
from cnlc_agent.domain.errors import DataError
from cnlc_agent.domain.execution import TERMINAL_EXECUTION_STATUSES, Execution
from cnlc_agent.domain.models import Contract
from cnlc_agent.domain.session_binding import TaskSessionIdentity

_SCOPE_ADAPTER: TypeAdapter[OperationScope] = TypeAdapter(OperationScope)


class IntervalIdentitySource(StrEnum):
    """身份来源；派生身份仅在本 Execution 内稳定，不是数据库主键。"""

    NATIVE = "NATIVE"
    EXECUTION_ORDINAL = "EXECUTION_ORDINAL"


class IntervalIdentity(Contract):
    """版本内层段身份；原生 ID 也限定版本，避免重复原生 ID 导致跨版本误用。"""

    model_config = ConfigDict(frozen=True)

    execution_id: str
    interval_id: str
    ordinal: int = Field(ge=1)
    top_depth: float
    bottom_depth: float
    identity_source: IntervalIdentitySource
    native_interval_id: str | None = None


class DepthCoverage(Contract):
    """真实原始数据的覆盖边界；不表示区间内每个点都有样本。"""

    model_config = ConfigDict(frozen=True)

    top: float
    bottom: float
    depth_reference: DepthReference
    unit: str = "m"


class ResolvedScope(Contract):
    """范围结果显式绑定任务和版本，空筛选与未解析筛选保持不同语义。"""

    task_id: str
    execution_id: str
    scope: OperationScope
    intervals: list[IntervalIdentity] = Field(default_factory=list)
    depth_coverage: DepthCoverage | None = None
    resolution_source: OperationScopeKind


def _depth(value: JsonValue | None) -> float:
    """只接受快照真实数值；坏层段不能被跳过，否则 ordinal 会悄悄漂移。"""

    if isinstance(value, bool) or not isinstance(value, (float, int)) or not isfinite(value):
        raise DataError("AMBIGUOUS_SCOPE", "层段深度结构无效，无法可靠构建范围")
    return float(value)


class IntervalIndex:
    """从终态快照建立版本内索引；不写回历史 State，不承诺跨版本地质层等价。"""

    def __init__(self, execution: Execution) -> None:
        # 非终态快照仍可能改变层段顺序，不能为它承诺稳定的 ordinal 身份。
        if execution.status not in TERMINAL_EXECUTION_STATUSES:
            raise DataError("STALE_CONTEXT_REFERENCE", "执行尚未结束，层段快照还未固定")
        stage = execution.state_snapshot.interval_result
        rows = stage.result.get("intervals") if stage is not None else None
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise DataError("AMBIGUOUS_SCOPE", "层段集合结构无效")
        self._items: dict[str, IntervalIdentity] = {}
        for ordinal, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise DataError("AMBIGUOUS_SCOPE", "层段结构无效，不能跳过后继续编号")
            top, bottom = _depth(row.get("top_depth_m")), _depth(row.get("bottom_depth_m"))
            if top >= bottom:
                raise DataError("AMBIGUOUS_SCOPE", "层段顶底深度无效")
            native = row.get("interval_id")
            if native is not None and (not isinstance(native, str) or not native.strip()):
                raise DataError("AMBIGUOUS_SCOPE", "原生层段标识无效")
            # 各部分分别编码并区分来源，防止 ID 分隔符或 native/ordinal 命名碰撞。
            suffix = (
                f"native:{quote(native, safe='')}"
                if isinstance(native, str)
                else f"ordinal:{ordinal}"
            )
            interval_id = f"interval:{quote(execution.execution_id, safe='')}:{suffix}"
            if interval_id in self._items:
                raise DataError("AMBIGUOUS_SCOPE", "当前版本存在重复原生层段标识")
            self._items[interval_id] = IntervalIdentity(
                execution_id=execution.execution_id,
                interval_id=interval_id,
                ordinal=ordinal,
                top_depth=top,
                bottom_depth=bottom,
                identity_source=(
                    IntervalIdentitySource.NATIVE
                    if native is not None
                    else IntervalIdentitySource.EXECUTION_ORDINAL
                ),
                native_interval_id=native,
            )

    def identities(self) -> tuple[IntervalIdentity, ...]:
        """按快照原始顺序返回只读身份，不按深度重新排序。"""

        return tuple(self._items.values())

    def resolve_id(self, interval_id: str) -> IntervalIdentity:
        """只接受本版本索引中的限定 ID，不按相似深度或原生 ID 自动补匹配。"""

        item = self._items.get(interval_id)
        if item is None:
            raise DataError("INTERVAL_NOT_FOUND", "当前版本不存在指定层段")
        return item

    def resolve_ordinal(self, ordinal: int) -> IntervalIdentity:
        """一基序号仅用于本版本定位；无效序号不得借用 Python 的负索引。"""

        if type(ordinal) is not int or not 1 <= ordinal <= len(self._items):
            raise DataError("INTERVAL_NOT_FOUND", "当前版本不存在指定层序号")
        return self.identities()[ordinal - 1]


def resolve_interval_ordinal(execution: Execution, ordinal: int) -> IntervalIdentity:
    """供服务端可信 Execution 使用的纯函数；外部输入应先经 ScopeResolver 授权。"""

    return IntervalIndex(execution).resolve_ordinal(ordinal)


class ScopeResolver:
    """只通过 Repository 读取快照，重新校验任务和版本归属，不接 Redis。"""

    def __init__(self, repository: TaskRepository, session_identity: TaskSessionIdentity) -> None:
        self.references = OperationReferenceResolver(repository, session_identity)

    async def resolve(
        self, task_id: str, execution_id: str, scope: OperationScope
    ) -> ResolvedScope:
        """所有层段必须完整解析；深度超界不裁剪，筛选表达式不求值。"""

        scope = _SCOPE_ADAPTER.validate_python(scope.model_dump())
        execution = await self.references.load_execution(task_id, execution_id)
        intervals: list[IntervalIdentity] = []
        coverage = None
        if isinstance(scope, (DepthRangeScope, DepthPointScope)):
            raw = execution.state_snapshot.raw_data
            if raw is None or not raw.depths or scope.depth_reference != raw.depth_reference:
                raise DataError("DEPTH_REFERENCE_UNAVAILABLE", "当前版本没有请求基准的深度数据")
            coverage = DepthCoverage(
                top=min(raw.depths),
                bottom=max(raw.depths),
                depth_reference=DepthReference(raw.depth_reference),
                unit=raw.depth_unit,
            )
            top = scope.top if isinstance(scope, DepthRangeScope) else scope.depth
            bottom = scope.bottom if isinstance(scope, DepthRangeScope) else scope.depth
            if top < coverage.top or bottom > coverage.bottom:
                raise DataError("DEPTH_OUT_OF_RANGE", "请求深度超出当前版本的真实数据覆盖")
        elif isinstance(scope, (IntervalScope, MultiIntervalScope, FilterSetScope)):
            if isinstance(scope, FilterSetScope) and scope.resolved_ids is None:
                raise DataError("FILTER_SET_UNRESOLVED", "条件筛选集合尚未冻结")
            ids = (
                [scope.interval_id]
                if isinstance(scope, IntervalScope)
                else scope.interval_ids
                if isinstance(scope, MultiIntervalScope)
                else scope.resolved_ids
            )
            assert ids is not None
            # 空的已冻结筛选无需层段存在；是否可执行由后续 PlanValidator 判断。
            if ids:
                index = IntervalIndex(execution)
                intervals = [index.resolve_id(item) for item in dict.fromkeys(ids)]
        return ResolvedScope(
            task_id=task_id,
            execution_id=execution_id,
            scope=scope,
            intervals=intervals,
            depth_coverage=coverage,
            resolution_source=scope.kind,
        )

    async def resolve_reference(
        self, task_id: str, execution_id: str, scope: PartialScope
    ) -> ResolvedScope:
        """在已选版本内原子规范化层号；任一层不存在则整个范围失败。"""
        if isinstance(scope, IntervalOrdinalReference):
            item = await self.resolve_interval_ordinal(task_id, execution_id, scope.ordinal)
            scope = IntervalScope(interval_id=item.interval_id)
        elif isinstance(scope, MultiIntervalOrdinalReference):
            items = [
                await self.resolve_interval_ordinal(task_id, execution_id, ordinal)
                for ordinal in scope.ordinals
            ]
            scope = MultiIntervalScope(interval_ids=[item.interval_id for item in items])
        return await self.resolve(task_id, execution_id, scope)

    async def resolve_interval_ordinal(
        self, task_id: str, execution_id: str, ordinal: int
    ) -> IntervalIdentity:
        """经会话归属核验后解析层序号，不能由 LLM 构造 Execution 绕过授权。"""

        execution = await self.references.load_execution(task_id, execution_id)
        return resolve_interval_ordinal(execution, ordinal)
