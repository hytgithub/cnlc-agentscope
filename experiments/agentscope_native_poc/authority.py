"""Task 012B 的隔离权威 Fixture；不读取生产 Resolver 或持久化。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionDecision
from agentscope.tool import ParamsBase, ToolBase, ToolChunk
from pydantic import Field, ValidationError


@dataclass(frozen=True)
class AuthorityContext:
    """服务端式 Fixture 当前权威锚点；其值不从对话历史恢复。"""

    session_id: str
    task_id: str
    well_id: str
    well_name: str
    current_execution_id: str
    previous_execution_id: str
    scope_ref: str
    scope_label: str
    revision: int
    known_execution_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """产生可记录的确定性权威快照。"""

        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "well_id": self.well_id,
            "well_name": self.well_name,
            "current_execution_id": self.current_execution_id,
            "previous_execution_id": self.previous_execution_id,
            "scope_ref": self.scope_ref,
            "scope_label": self.scope_label,
            "revision": self.revision,
            "known_execution_ids": list(self.known_execution_ids),
            "source": "task_012b_fixture",
            "is_real_business_data": False,
        }


@dataclass
class AuthorityFixture:
    """独立的 Session → Task/Well/Execution 映射，模拟权威 Resolver 边界。"""

    session_id: str = "SESSION-012B"
    active_well_id: str = "WELL-A"
    allowed_switch_well: str | None = None
    profiles: dict[str, AuthorityContext] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def standard(cls) -> AuthorityFixture:
        """建立两个虚构井和不同版本的会话授权范围。"""

        return cls(
            profiles={
                "WELL-A": AuthorityContext(
                    session_id="SESSION-012B",
                    task_id="TASK-A",
                    well_id="WELL-A",
                    well_name="测试井 A",
                    current_execution_id="A-V2",
                    previous_execution_id="A-V1",
                    scope_ref="A-INTERVAL-2035-2038",
                    scope_label="2035–2038m",
                    revision=2,
                    known_execution_ids=("A-V1", "A-V2"),
                ),
                "WELL-B": AuthorityContext(
                    session_id="SESSION-012B",
                    task_id="TASK-B",
                    well_id="WELL-B",
                    well_name="测试井 B",
                    current_execution_id="B-V8",
                    previous_execution_id="B-V7",
                    scope_ref="B-INTERVAL-1020-1024",
                    scope_label="1020–1024m",
                    revision=8,
                    known_execution_ids=("B-V7", "B-V8"),
                ),
            },
        )

    @property
    def current(self) -> AuthorityContext:
        """读取当前权威任务；对话中提到的旧任务不改变此选择。"""

        return self.profiles[self.active_well_id]

    def switch_well(self, well_id: str) -> dict[str, Any]:
        """只允许执行场景显式授权的切换，不从旧对话自行复活任务。"""

        before = self.current.as_dict()
        if well_id not in self.profiles:
            result = {"status": "NOT_FOUND", "requested_well_id": well_id}
        elif self.allowed_switch_well != well_id:
            result = {
                "status": "REJECTED",
                "requested_well_id": well_id,
                "active_well_id": self.active_well_id,
                "message": "本轮没有明确的 Session 切井授权；当前 Authority 未改变。",
            }
        else:
            self.active_well_id = well_id
            self.allowed_switch_well = None
            result = {"status": "OK", "authority": self.current.as_dict()}
        self.trace.append({"operation": "switch_well", "before": before, "result": result})
        return result

    def bind(
        self,
        *,
        well_reference: str | None,
        version_selector: Literal["CURRENT", "PREVIOUS"],
        scope_reference: str | None,
        requested_task_id: str | None = None,
        requested_execution_id: str | None = None,
    ) -> dict[str, Any]:
        """将语言候选绑定到当前 Authority；结果字段只来自 Fixture。"""

        authority = self.current
        normalized_well = _normalize_well_reference(well_reference)
        mismatch = bool(normalized_well and normalized_well != authority.well_id)
        requested_scope = _normalize_scope_reference(scope_reference)
        scope_mismatch = bool(requested_scope and requested_scope != authority.scope_ref)
        selected_execution = (
            authority.previous_execution_id
            if version_selector == "PREVIOUS"
            else authority.current_execution_id
        )
        requested_version_mismatch = bool(
            requested_execution_id and requested_execution_id != selected_execution
        )
        result = {
            "status": "OK",
            "binding_status": "AUTHORITY_WINS",
            "candidate_mismatch": {
                "well": mismatch,
                "task": bool(requested_task_id and requested_task_id != authority.task_id),
                "version": requested_version_mismatch,
                "scope": scope_mismatch,
            },
            "authority": authority.as_dict(),
            "bound": {
                "task_id": authority.task_id,
                "well_id": authority.well_id,
                "execution_id": selected_execution,
                "version_selector": version_selector,
                "scope_ref": authority.scope_ref,
                "scope_label": authority.scope_label,
                "revision": authority.revision,
            },
            "summary": "Task 012B 模拟查询；只含合成 Fixture，不含真实解释结果。",
        }
        self.trace.append(
            {
                "operation": "bind_and_query",
                "candidates": {
                    "well_reference": well_reference,
                    "task_id": requested_task_id,
                    "execution_id": requested_execution_id,
                    "scope_reference": scope_reference,
                    "version_selector": version_selector,
                },
                "result": result,
            },
        )
        return result

    def activate_authority(self, well_id: str) -> dict[str, Any]:
        """模拟外部会话绑定变化；只由实验脚本调用，不作为 Agent Tool 暴露。"""

        if well_id not in self.profiles:
            raise ValueError(f"unknown fixture well: {well_id}")
        before = self.current.as_dict()
        self.active_well_id = well_id
        self.trace.append(
            {
                "operation": "external_authority_change",
                "before": before,
                "result": {"status": "OK", "authority": self.current.as_dict()},
            },
        )
        return self.current.as_dict()

    def preflight_local_modify(
        self, target: str | None, value: float | None, scope_reference: str | None
    ) -> dict[str, Any]:
        """拒绝层段级写入；查看范围永不被升级成全井写入范围。"""

        result: dict[str, Any]
        if not target or value is None:
            result = {
                "status": "NEED_CLARIFICATION",
                "message": "缺少参数或目标值；没有执行修改。",
            }
        else:
            result = {
                "status": "UNSUPPORTED",
                "message": "Task 012B POC 不开放层段参数修改；未创建写操作。",
                "requested_scope": _normalize_scope_reference(scope_reference),
                "authority_scope_ref": self.current.scope_ref,
                "write_scope": None,
            }
        self.trace.append(
            {
                "operation": "preflight_local_modify",
                "authority": self.current.as_dict(),
                "result": result,
            },
        )
        return result


def _normalize_well_reference(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().upper().replace(" ", "")
    if "A" in normalized or "测试井甲" in normalized:
        return "WELL-A"
    if "B" in normalized or "测试井乙" in normalized:
        return "WELL-B"
    return normalized


def _normalize_scope_reference(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.replace("–", "-").replace("—", "-").replace(" ", "")
    if "2035" in normalized and "2038" in normalized:
        return "A-INTERVAL-2035-2038"
    if "1020" in normalized and "1024" in normalized:
        return "B-INTERVAL-1020-1024"
    if normalized in {"A-INTERVAL-2035-2038", "B-INTERVAL-1020-1024"}:
        return normalized
    return normalized


class _BaseAuthorityTool(ToolBase):
    """将模型候选交给隔离 Fixture，并保留每次 Tool 输入/输出。"""

    input_model: ClassVar[type[ParamsBase]]
    is_concurrency_safe = False
    is_read_only = True

    def __init__(self, fixture: AuthorityFixture) -> None:
        super().__init__()
        self.fixture = fixture
        self.input_schema = self.input_model.model_json_schema()

    async def check_permissions(
        self, tool_input: dict[str, Any], context: PermissionContext
    ) -> PermissionDecision:
        del tool_input, context
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Task 012B 仅读写本地合成 Fixture。",
        )

    async def call(self, *args: Any, **kwargs: Any) -> ToolChunk:
        if args:
            parsed_result: dict[str, Any] = {"status": "INVALID_INPUT"}
            args_record: dict[str, Any] = {"positional_argument_count": len(args)}
        else:
            try:
                parsed = self.input_model.model_validate(kwargs)
            except ValidationError:
                parsed_result = {"status": "INVALID_INPUT"}
                args_record = dict(kwargs)
            else:
                args_record = parsed.model_dump(mode="json", exclude_unset=True)
                parsed_result = self.execute(parsed)
        self.fixture.trace.append(
            {"tool_name": self.name, "arguments": args_record, "result": parsed_result}
        )
        state = (
            ToolResultState.ERROR
            if parsed_result.get("status") in {"INVALID_INPUT", "NOT_FOUND", "REJECTED"}
            else ToolResultState.SUCCESS
        )
        return ToolChunk(
            content=[TextBlock(text=json.dumps(parsed_result, ensure_ascii=False))],
            state=state,
            metadata={"result": parsed_result, "fixture": True},
        )

    def execute(self, parsed: ParamsBase) -> dict[str, Any]:
        raise NotImplementedError


class EmptyAuthorityInput(ParamsBase):
    """不需要候选参数的当前权威上下文查询。"""


class BindQueryInput(ParamsBase):
    """语言候选字段；返回事实必须由 AuthorityFixture 绑定。"""

    well_reference: str | None = Field(default=None, description="用户指代的井候选")
    version_selector: Literal["CURRENT", "PREVIOUS"] = "CURRENT"
    scope_reference: str | None = Field(default=None, description="用户指代的层段候选")
    requested_task_id: str | None = Field(default=None, description="仅供冲突审计")
    requested_execution_id: str | None = Field(default=None, description="仅供冲突审计")


class SwitchWellInput(ParamsBase):
    """切换到会话内明确指定的虚构井。"""

    well_id: str


class LocalModifyInput(ParamsBase):
    """层段级参数修改预检候选。"""

    target: str | None = None
    value: float | None = None
    scope_reference: str | None = None


class GetAuthorityTool(_BaseAuthorityTool):
    """返回只读的当前权威井、Task、版本和范围。"""

    name = "get_authority_context"
    description = "读取当前 Session 权威井、Task、Execution 版本、范围与 revision。"
    input_model = EmptyAuthorityInput

    def execute(self, parsed: ParamsBase) -> dict[str, Any]:
        del parsed
        return {"status": "OK", "authority": self.fixture.current.as_dict()}


class BindQueryTool(_BaseAuthorityTool):
    """把用户语言中的井/范围/版本候选交由 Authority Fixture 绑定。"""

    name = "query_authority_result"
    description = (
        "只读查询。对‘上一版’传 version_selector=PREVIOUS；传入的井、范围、Task、"
        "Execution 都只是候选，最终绑定必须使用返回的 authority 与 bound 字段。"
    )
    input_model = BindQueryInput

    def execute(self, parsed: ParamsBase) -> dict[str, Any]:
        request = BindQueryInput.model_validate(parsed)
        return self.fixture.bind(**request.model_dump())


class SwitchWellTool(_BaseAuthorityTool):
    """仅用于演示 session 内显式切换，不接受对话记忆作权威绑定。"""

    name = "switch_session_well"
    description = "显式切换到当前 Session 已绑定的 WELL-A 或 WELL-B。"
    input_model = SwitchWellInput
    is_read_only = False

    def execute(self, parsed: ParamsBase) -> dict[str, Any]:
        request = SwitchWellInput.model_validate(parsed)
        return self.fixture.switch_well(request.well_id)


class PreflightLocalModifyTool(_BaseAuthorityTool):
    """所有层段级参数修改均返回 UNSUPPORTED，不暴露 Apply Tool。"""

    name = "preflight_local_modify"
    description = "预检层段参数修改；Task 012B 不支持局部修改，必须如实返回 UNSUPPORTED。"
    input_model = LocalModifyInput
    is_read_only = False

    def execute(self, parsed: ParamsBase) -> dict[str, Any]:
        request = LocalModifyInput.model_validate(parsed)
        return self.fixture.preflight_local_modify(
            request.target, request.value, request.scope_reference
        )


def build_authority_tools(fixture: AuthorityFixture) -> list[ToolBase]:
    """为单个实验 Session 建立不可跨用例共享的 Authority Toolkit。"""

    return [
        GetAuthorityTool(fixture),
        BindQueryTool(fixture),
        SwitchWellTool(fixture),
        PreflightLocalModifyTool(fixture),
    ]
