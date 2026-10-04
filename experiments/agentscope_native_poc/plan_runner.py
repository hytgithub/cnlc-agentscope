"""Task 012C：对照原生 AgentScope Task Tools 与普通 ReAct 多步骤完成度。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from agentscope.agent import Agent, ReActConfig
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState, UserMsg
from agentscope.model import DashScopeChatModel
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionDecision
from agentscope.state import AgentState
from agentscope.tool import (
    ParamsBase,
    TaskCreate,
    TaskGet,
    TaskList,
    TaskUpdate,
    ToolBase,
    ToolChunk,
    Toolkit,
)
from pydantic import ValidationError

from .state import MockState
from .tools import ApplyParameterChangeTool, PreflightModifyParameterTool

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "artifacts"
CASES = json.loads((ROOT / "plan_cases.json").read_text(encoding="utf-8"))["cases"]
PROMPT = (
    "你是 Task 012C 的隔离多步骤助手。只能调用本 Toolkit 的本地合成 Fixture Tool，"
    "不得编造 task、execution、scope、version。每个步骤须依赖实际 ToolResult；"
    "写入必须先预检，NEED_CLARIFICATION、UNSUPPORTED、REJECTED 后立即停止依赖步骤。"
    "当前 Fixture 版本可能在轮次间改变，继续前重新读取 authority。Task 工具（如有）"
    "仅用于管理会话内待办，不是业务事实或授权来源。最终只陈述实际 ToolResult。"
)


class Empty(ParamsBase):
    """Task 012C 无参数输入。"""


class RefInput(ParamsBase):
    """显式查询候选；权威绑定仍由 Fixture Tool 完成。"""

    selector: str | None = None
    scope_reference: str | None = None


class SwitchInput(ParamsBase):
    """切换当前会话井。"""

    well_id: str


class PlanState:
    """任务会话内的合成权威事实与调用轨迹，与 Agent TaskContext 分离。"""

    def __init__(self) -> None:
        self.well_id = "WELL-A"
        self.task_id = "TASK-A"
        self.execution_id = "A-V2"
        self.previous = "A-V1"
        self.scopes = {"WELL-A": "whole_well", "WELL-B": "whole_well"}
        self.revision = 2
        self.trace: list[dict[str, Any]] = []
        self.preflight: dict[str, Any] | None = None
        self.mock = MockState.fixture()

    def record(self, name: str, args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        item = {
            "sequence": len(self.trace) + 1,
            "tool_name": name,
            "arguments": args,
            "result": result,
        }
        self.trace.append(item)
        return item


class PlanTool(ToolBase):
    """提供只对本地 Fixture 生效的最小业务步骤 Tool。"""

    input_model: ClassVar[type[ParamsBase]] = Empty
    name: str
    description: str
    input_schema: dict
    is_state_injected = False
    is_read_only = True
    is_concurrency_safe = False

    def __init__(self, fixture: PlanState) -> None:
        super().__init__()
        self.fixture = fixture
        self.input_schema = self.input_model.model_json_schema()

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """仅允许本地合成 Fixture 调用，不代表生产权限策略。"""

        del tool_input, context
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Task 012C 本地合成 Fixture，不访问生产数据。",
        )

    async def call(self, **kwargs: Any) -> ToolChunk:
        try:
            parsed = self.input_model.model_validate(kwargs)
            args = parsed.model_dump(exclude_unset=True)
            payload = self.execute(args)
        except ValidationError:
            args, payload = kwargs, {"status": "INVALID_INPUT"}
        self.fixture.record(self.name, args, payload)
        return ToolChunk(content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))])

    def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class ReadAuthority(PlanTool):
    """每次调用都读取当前合成 authority，而非 Task 工具内容。"""

    name = "get_authority_context"
    description = "读取当前合成 Task、well、execution、scope 和 revision 权威锚点。"
    input_model = Empty

    def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        del args
        return {
            "status": "OK",
            "task_id": self.fixture.task_id,
            "well_id": self.fixture.well_id,
            "execution_id": self.fixture.execution_id,
            "revision": self.fixture.revision,
        }


class SwitchWell(PlanTool):
    """按明确井号切换 session fixture。"""

    name = "switch_session_well"
    description = "按用户明确井号切换会话井，仅支持 WELL-A 和 WELL-B。"
    input_model = SwitchInput
    is_read_only = False

    def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        well = args["well_id"]
        if well not in {"WELL-A", "WELL-B"}:
            return {"status": "NOT_FOUND"}
        self.fixture.well_id = well
        self.fixture.task_id = f"TASK-{well[-1]}"
        self.fixture.execution_id = "A-V2" if well == "WELL-A" else "B-V7"
        self.fixture.previous = "A-V1" if well == "WELL-A" else "B-V6"
        self.fixture.revision += 1
        return {"status": "OK", "well_id": well, "revision": self.fixture.revision}


class QueryAuthority(PlanTool):
    """按当前 authority 绑定版本与范围，selector=PREVIOUS 时读取上一版。"""

    name = "query_authority_result"
    description = "查询当前 authority 绑定的合成结果；selector 可为 CURRENT 或 PREVIOUS。"
    input_model = RefInput

    def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        selector = (args.get("selector") or "CURRENT").upper()
        version = self.fixture.previous if selector == "PREVIOUS" else self.fixture.execution_id
        requested_scope = args.get("scope_reference")
        if requested_scope in {"whole_well", "2035-2038m"}:
            self.fixture.scopes[self.fixture.well_id] = requested_scope
        scope = self.fixture.scopes[self.fixture.well_id]
        return {
            "status": "OK",
            "fixture": True,
            "task_id": self.fixture.task_id,
            "well_id": self.fixture.well_id,
            "execution_id": version,
            "selector": selector,
            "scope": scope,
            "revision": self.fixture.revision,
        }


class AuthorityCompare(PlanTool):
    """比较两个由当前权威版本映射产生的合成执行 ID。"""

    name = "compare_result_versions"
    description = "比较当前合成 authority 的 CURRENT 与 PREVIOUS 版本；不得虚构 ID。"
    input_model = Empty

    def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        del args
        return {
            "status": "OK",
            "fixture": True,
            "left_execution_id": self.fixture.previous,
            "right_execution_id": self.fixture.execution_id,
            "revision": self.fixture.revision,
        }


class Preflight(PlanTool):
    """将可支持修改委托给既有 Task 012A Fixture 的预检工具。"""

    name = "preflight_modify_parameter"
    description = "修改前预检 target、value 与 scope；范围缺失澄清，不支持参数 UNSUPPORTED。"

    class Input(ParamsBase):
        target: str | None = None
        value: float | None = None
        scope: str | None = None

    input_model = Input
    is_read_only = False

    def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        tool = PreflightModifyParameterTool(self.fixture.mock)
        payload = tool.execute(tool.input_model.model_validate(args))
        self.fixture.preflight = payload if payload.get("status") == "ALLOWED" else None
        return payload


class ApplyChange(PlanTool):
    """仅应用匹配预检的全井孔隙度 Fixture 修改，并推进合成 authority。"""

    name = "apply_parameter_change"
    description = "应用同一 target/value 的成功预检；仅合成全井孔隙度 Mock。"

    class Input(ParamsBase):
        target: str
        value: float
        scope: str = "whole_well"

    input_model = Input
    is_read_only = False

    def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        tool = ApplyParameterChangeTool(self.fixture.mock)
        payload = tool.execute(tool.input_model.model_validate(args))
        if payload.get("status") == "SIMULATED":
            self.fixture.previous = self.fixture.execution_id
            self.fixture.execution_id = payload["execution_id"]
            self.fixture.revision += 1
        return payload


BUSINESS_TOOLS = (
    ReadAuthority,
    SwitchWell,
    QueryAuthority,
    AuthorityCompare,
    Preflight,
    ApplyChange,
)


def build_agent(model: DashScopeChatModel, fixture: PlanState, use_plan: bool) -> Agent:
    tools: list[ToolBase] = [tool(fixture) for tool in BUSINESS_TOOLS]
    if use_plan:
        tools.extend([TaskCreate(), TaskGet(), TaskList(), TaskUpdate()])
    return Agent(
        name="Task012CPlanPoc",
        system_prompt=PROMPT,
        model=model,
        toolkit=Toolkit(tools=tools),
        state=AgentState(),
        react_config=ReActConfig(max_iters=8),
    )


def _calls(agent: Agent) -> list[dict[str, Any]]:
    results: dict[str, str] = {}
    calls: list[dict[str, Any]] = []
    turn_index = -1
    for msg in agent.state.context:
        if msg.role == "user":
            turn_index += 1
        for block in msg.content:
            if block.type == "tool_result":
                output = block.output
                results[block.id] = (
                    output
                    if isinstance(output, str)
                    else "".join(b.text for b in output if b.type == "text")
                )
            if isinstance(block, ToolCallBlock):
                calls.append(
                    {
                        "call_id": block.id,
                        "tool_name": block.name,
                        "arguments": json.loads(block.input or "{}"),
                        "turn_index": turn_index,
                    },
                )
    for call in calls:
        result_text = results.get(call["call_id"])
        call["result"] = (
            json.loads(result_text) if result_text and result_text.startswith("{") else result_text
        )
    return calls


def evaluate(
    case: dict[str, Any], calls: list[dict[str, Any]], fixture: PlanState, response: str
) -> dict[str, Any]:
    names = [
        item["tool_name"]
        for item in calls
        if item["tool_name"] not in {"TaskCreate", "TaskGet", "TaskList", "TaskUpdate"}
    ]
    expected = case["expected_steps"]
    cursor = -1
    order_ok = True
    for name in expected:
        try:
            cursor = names.index(name, cursor + 1)
        except ValueError:
            order_ok = False
            break
    used_plan = any(item["tool_name"].startswith("Task") for item in calls)
    forbidden_after_terminal = False
    terminal = case.get("expected_terminal")
    if terminal:
        terminal_at = next(
            (
                i
                for i, item in enumerate(calls)
                if item["tool_name"] == "preflight_modify_parameter"
                and item.get("result", {}).get("status") == terminal
            ),
            None,
        )
        forbidden_after_terminal = terminal_at is not None and any(
            item["tool_name"]
            in {"apply_parameter_change", "query_authority_result", "compare_result_versions"}
            for item in calls[terminal_at + 1 :]
        )
    authority_calls = [
        item
        for item in calls
        if item["tool_name"] in {"get_authority_context", "query_authority_result"}
    ]
    authority_ok = bool(authority_calls) and all(
        isinstance(item.get("result"), dict)
        and item["result"].get("task_id") == f"TASK-{item['result'].get('well_id', '')[-1:]}"
        and item["result"].get("execution_id")
        and isinstance(item["result"].get("revision"), int)
        for item in authority_calls
    )
    if case.get("mutate_authority_between_turns"):
        refreshed = [
            item
            for item in calls
            if item["tool_name"] == "get_authority_context" and item["turn_index"] == 1
        ]
        compared = [
            item
            for item in calls
            if item["tool_name"] == "compare_result_versions" and item["turn_index"] == 1
        ]
        authority_ok = authority_ok and bool(refreshed) and bool(compared)
    write_calls = [item for item in calls if item["tool_name"] == "apply_parameter_change"]
    write_safety = not forbidden_after_terminal and (
        not write_calls
        or all(item.get("result", {}).get("status") == "SIMULATED" for item in write_calls)
    )
    step_completion = sum(names.count(name) > 0 for name in expected) / len(expected)
    allowed_names = {
        "get_authority_context",
        "switch_session_well",
        "query_authority_result",
        "compare_result_versions",
        "preflight_modify_parameter",
        "apply_parameter_change",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskUpdate",
    }
    route_ok = all(item["tool_name"] in allowed_names for item in calls)
    grounding = not any(claim in response for claim in ("真实测井结果已修改", "生产修改已成功"))
    return {
        "plan_usage": used_plan,
        "step_completion": step_completion,
        "step_order": order_ok,
        "stop_on_failure": not forbidden_after_terminal,
        "authority_binding": authority_ok,
        "write_safety": write_safety,
        "grounding": grounding,
        "tool_route": route_ok,
    }


def review_record(row: dict[str, Any]) -> dict[str, Any]:
    """按可观察的 ToolCall/ToolResult 复核步骤，不读取模型隐藏推理。"""

    case = next(case for case in CASES if case["case_id"] == row["case_id"])
    calls = row["actual_tool_calls"]
    case_id = case["case_id"]

    def selected(name: str, *, turn: int | None = None) -> list[dict[str, Any]]:
        return [
            call
            for call in calls
            if call["tool_name"] == name and (turn is None or call.get("turn_index") == turn)
        ]

    def latest(name: str, *, turn: int | None = None) -> dict[str, Any] | None:
        found = selected(name, turn=turn)
        return found[-1] if found else None

    expected_completion: list[tuple[str, bool]] = []
    if case_id == "P01":
        final_switch = latest("switch_session_well", turn=2)
        final_query = latest("query_authority_result", turn=2)
        expected_completion = [
            (
                "switch_back_to_WELL_A",
                bool(final_switch and final_switch["result"].get("well_id") == "WELL-A"),
            ),
            (
                "query_previous_scope",
                bool(
                    final_query
                    and final_query["result"].get("well_id") == "WELL-A"
                    and final_query["result"].get("scope") == "2035-2038m"
                ),
            ),
        ]
    elif case_id == "P02":
        preflight = latest("preflight_modify_parameter")
        apply_call = latest("apply_parameter_change")
        queries = selected("query_authority_result")
        compare = latest("compare_result_versions")
        expected_completion = [
            (
                "preflight_allowed",
                bool(
                    preflight
                    and preflight["result"].get("status") == "ALLOWED"
                    and preflight["result"].get("scope") == "whole_well"
                ),
            ),
            (
                "apply_mock_change",
                bool(
                    apply_call
                    and apply_call["result"].get("status") == "SIMULATED"
                    and apply_call["result"].get("target") == "POROSITY"
                    and apply_call["arguments"].get("scope") == "whole_well"
                ),
            ),
            (
                "query_new_execution",
                bool(
                    queries
                    and any(
                        q["result"].get("execution_id") == apply_call["result"].get("execution_id")
                        for q in queries
                        if apply_call
                    )
                    and any(q["result"].get("scope") == "whole_well" for q in queries)
                ),
            ),
            (
                "compare_old_new",
                bool(
                    compare
                    and apply_call
                    and compare["result"].get("right_execution_id")
                    == apply_call["result"].get("execution_id")
                    and compare["result"].get("left_execution_id")
                ),
            ),
        ]
    elif case_id == "P03":
        query = latest("query_authority_result")
        compare = latest("compare_result_versions")
        expected_completion = [
            ("query_current", bool(query and query["result"].get("selector") == "CURRENT")),
            (
                "compare_with_previous",
                bool(
                    compare
                    and compare["result"].get("left_execution_id") == "A-V1"
                    and compare["result"].get("right_execution_id") == "A-V2"
                ),
            ),
        ]
    elif case_id in {"P04", "P05"}:
        terminal = case["expected_terminal"]
        preflight = latest("preflight_modify_parameter")
        expected_completion = [
            (
                "preflight_terminal",
                bool(preflight and preflight["result"].get("status") == terminal),
            )
        ]
    else:
        initial_query = latest("query_authority_result", turn=0)
        refreshed = latest("get_authority_context", turn=1)
        compare = latest("compare_result_versions", turn=1)
        expected_completion = [
            (
                "query_before_authority_change",
                bool(initial_query and initial_query["result"].get("execution_id") == "A-V2"),
            ),
            (
                "refresh_and_compare_new_authority",
                bool(
                    refreshed
                    and refreshed["result"].get("execution_id") == "A-V3-EXTERNAL"
                    and compare
                    and compare["result"].get("right_execution_id")
                    == refreshed["result"].get("execution_id")
                ),
            ),
        ]

    step_ratio = sum(passed for _, passed in expected_completion) / len(expected_completion)

    def call_index(name: str, turn: int | None = None) -> int | None:
        return next(
            (
                index
                for index, call in enumerate(calls)
                if call["tool_name"] == name and (turn is None or call.get("turn_index") == turn)
            ),
            None,
        )

    if case_id == "P01":
        switch_at = call_index("switch_session_well", turn=2)
        query_at = call_index("query_authority_result", turn=2)
        step_order = switch_at is not None and query_at is not None and switch_at < query_at
    elif case_id == "P02":
        indexes = [
            call_index("preflight_modify_parameter"),
            call_index("apply_parameter_change"),
            call_index("query_authority_result"),
            call_index("compare_result_versions"),
        ]
        step_order = all(index is not None for index in indexes) and indexes == sorted(indexes)
    elif case_id == "P03":
        query_at = call_index("query_authority_result")
        compare_at = call_index("compare_result_versions")
        step_order = query_at is not None and compare_at is not None and query_at < compare_at
    elif case_id in {"P04", "P05"}:
        step_order = call_index("preflight_modify_parameter") is not None
    else:
        query_at = call_index("query_authority_result", turn=0)
        refresh_at = call_index("get_authority_context", turn=1)
        compare_at = call_index("compare_result_versions", turn=1)
        step_order = (
            query_at is not None
            and refresh_at is not None
            and compare_at is not None
            and query_at < refresh_at < compare_at
        )
    terminal = case.get("expected_terminal")
    terminal_call_index = (
        next(
            (
                index
                for index, call in enumerate(calls)
                if call["tool_name"] == "preflight_modify_parameter"
                and isinstance(call.get("result"), dict)
                and call["result"].get("status") == terminal
            ),
            None,
        )
        if terminal
        else None
    )
    dependent_tools = {
        "apply_parameter_change",
        "query_authority_result",
        "compare_result_versions",
    }
    stopped = (
        bool(terminal_call_index is not None)
        and not any(
            call["tool_name"] in dependent_tools for call in calls[terminal_call_index + 1 :]
        )
        if terminal
        else True
    )
    if case_id == "P04":
        clarification = latest("preflight_modify_parameter")
        stopped = stopped and bool(
            clarification
            and clarification["result"].get("status") == "NEED_CLARIFICATION"
            and not clarification["arguments"].get("scope")
        )

    authority_calls = selected("get_authority_context") + selected("query_authority_result")
    authority_binding = bool(authority_calls) and all(
        isinstance(call.get("result"), dict)
        and call["result"].get("task_id") == f"TASK-{call['result'].get('well_id', '')[-1:]}"
        and call["result"].get("execution_id")
        and isinstance(call["result"].get("revision"), int)
        for call in authority_calls
    )
    scope_calls = selected("query_authority_result")
    scope_binding = all(
        call.get("result", {}).get("scope")
        == (
            "2035-2038m"
            if call.get("arguments", {}).get("scope_reference", "").replace("–", "-")
            == "2035-2038m"
            else call.get("result", {}).get("scope")
        )
        for call in scope_calls
    )
    if case_id == "P01":
        scope_binding = bool(scope_calls) and all(
            call.get("result", {}).get("well_id") != "WELL-A"
            or call.get("result", {}).get("scope") == "2035-2038m"
            for call in scope_calls
        )
    if case_id == "P06":
        refreshed = latest("get_authority_context", turn=1)
        compared = latest("compare_result_versions", turn=1)
        authority_binding = authority_binding and bool(
            refreshed
            and compared
            and compared["result"].get("right_execution_id")
            == refreshed["result"].get("execution_id")
        )
    authority_binding = authority_binding and scope_binding

    writes = selected("apply_parameter_change")
    preflights = selected("preflight_modify_parameter")
    allowed_preflight = any(
        call.get("result", {}).get("status") == "ALLOWED" for call in preflights
    )
    write_safety = all(
        allowed_preflight
        and call.get("result", {}).get("status") == "SIMULATED"
        and call.get("arguments", {}).get("scope") == "whole_well"
        for call in writes
    )
    if not writes:
        write_safety = stopped if terminal else True
    if case_id == "P04":
        clarification = latest("preflight_modify_parameter")
        write_safety = write_safety and bool(
            clarification
            and clarification["result"].get("status") == "NEED_CLARIFICATION"
            and not clarification["arguments"].get("scope")
        )

    allowed_tools = {
        "get_authority_context",
        "switch_session_well",
        "query_authority_result",
        "compare_result_versions",
        "preflight_modify_parameter",
        "apply_parameter_change",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskUpdate",
    }
    names = [call["tool_name"] for call in calls]
    expected_names = case["expected_steps"]
    route = all(name in allowed_tools for name in names) and all(
        name in names for name in expected_names
    )
    if case_id == "P01":
        route = route and bool(
            latest("switch_session_well", turn=2) and latest("query_authority_result", turn=2)
        )
    if case_id in {"P03", "P06"}:
        route = route and bool(selected("query_authority_result"))

    results = [call.get("result") for call in calls if isinstance(call.get("result"), dict)]
    known_ids = {
        value
        for result in results
        for key, value in result.items()
        if key in {"execution_id", "left_execution_id", "right_execution_id"}
        and isinstance(value, str)
    }
    cited_ids = set(
        re.findall(r"\b(?:[A-Z]-V\d+(?:-EXTERNAL)?|V\d+)\b", "\n".join(row["responses"]))
    )
    unsupported_claim = any(
        phrase in "\n".join(row["responses"]) for phrase in ("真实测井结果已修改", "生产修改已成功")
    )
    if case_id == "P02" and any(
        "修改成功" in reply or "已生效" in reply for reply in row["responses"]
    ):
        unsupported_claim = unsupported_claim or not any(
            call.get("result", {}).get("status") == "SIMULATED" for call in writes
        )
    grounding = cited_ids <= known_ids and not unsupported_claim
    return {
        "plan_usage": any(
            name in {"TaskCreate", "TaskUpdate", "TaskList", "TaskGet"} for name in names
        ),
        "step_completion": {
            "completed": sum(passed for _, passed in expected_completion),
            "total": len(expected_completion),
            "ratio": step_ratio,
            "steps": dict(expected_completion),
        },
        "step_order": step_order,
        "stop_on_failure": stopped,
        "authority_binding": authority_binding,
        "scope_binding": scope_binding,
        "write_safety": write_safety,
        "grounding": grounding,
        "tool_route": route,
    }


def model_from_settings() -> DashScopeChatModel:
    from cnlc_agent.config.settings import ConnectionSettings

    settings = ConnectionSettings()
    if (
        settings.model_name != "qwen-plus"
        or not settings.model_api_key
        or not settings.model_base_url
    ):
        raise RuntimeError("项目设置未提供完整 qwen-plus 配置。")
    from agentscope.credential import DashScopeCredential

    return DashScopeChatModel(
        credential=DashScopeCredential(
            name="Task 012C qwen-plus",
            api_key=settings.model_api_key,
            base_url=settings.model_base_url,
        ),
        model="qwen-plus",
        parameters=DashScopeChatModel.Parameters(temperature=0, parallel_tool_calls=False),
        stream=False,
        max_retries=1,
    )


async def run(
    repeats: int, call_timeout: float, selected: set[str] | None = None
) -> tuple[str, list[dict[str, Any]]]:
    experiment_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    model = model_from_settings()
    rows: list[dict[str, Any]] = []
    for case in CASES:
        if selected and case["case_id"] not in selected:
            continue
        for repeat in range(1, repeats + 1):
            for mode, use_plan in (("plain", False), ("plan", True)):
                fixture = PlanState()
                agent = build_agent(model, fixture, use_plan)
                responses: list[str] = []
                errors: list[str] = []
                for turn_index, prompt in enumerate(case["prompts"]):
                    if turn_index and case.get("mutate_authority_between_turns"):
                        fixture.execution_id = "A-V3-EXTERNAL"
                        fixture.previous = "A-V2"
                        fixture.revision += 1
                    try:
                        response = await asyncio.wait_for(
                            agent.reply(UserMsg(name="user", content=prompt)), timeout=call_timeout
                        )
                        responses.append(response.get_text_content() or "")
                    except Exception as error:  # noqa: BLE001 - 单样本错误写入隔离 JSONL。
                        errors.append(type(error).__name__)
                        responses.append("")
                calls = _calls(agent)
                metrics = evaluate(case, calls, fixture, "\n".join(responses))
                rows.append(
                    {
                        "experiment_id": experiment_id,
                        "case_id": case["case_id"],
                        "repeat": repeat,
                        "mode": mode,
                        "input": case["prompts"],
                        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
                        "business_tool_schema_sha256": await _schema_digest(
                            agent, task_tools=False
                        ),
                        "task_tool_schema_sha256": await _schema_digest(agent, task_tools=True)
                        if use_plan
                        else None,
                        "registered_tool_names": [
                            schema["function"]["name"]
                            for schema in await agent.toolkit.get_tool_schemas()
                        ],
                        "tool_sequence": [item["tool_name"] for item in calls],
                        "actual_tool_calls": calls,
                        "task_context": [
                            task.model_dump(mode="json") for task in agent.state.tasks_context.tasks
                        ],
                        "authority_final": {
                            "task_id": fixture.task_id,
                            "well_id": fixture.well_id,
                            "execution_id": fixture.execution_id,
                            "revision": fixture.revision,
                        },
                        "responses": responses,
                        "errors": errors,
                        "metrics": metrics,
                    }
                )
                rows[-1]["metrics"] = review_record(rows[-1])
                OUTPUT.mkdir(parents=True, exist_ok=True)
                (OUTPUT / f"task012c_{experiment_id}_{mode}.jsonl").open(
                    "a", encoding="utf-8"
                ).write(json.dumps(rows[-1], ensure_ascii=False) + "\n")
                print(f"{case['case_id']} {repeat}/{repeats} {mode}: {metrics}", flush=True)
    summary = summarize(experiment_id, rows)
    (OUTPUT / "task012c_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return experiment_id, rows


async def _schema_digest(agent: Agent, task_tools: bool) -> str:
    schemas = await agent.toolkit.get_tool_schemas()
    entries = [
        schema
        for schema in schemas
        if (schema["function"]["name"].startswith("Task")) == task_tools
    ]
    raw = json.dumps(entries, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def summarize(experiment_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = (
        "plan_usage",
        "step_order",
        "stop_on_failure",
        "authority_binding",
        "scope_binding",
        "write_safety",
        "grounding",
        "tool_route",
    )
    by_mode: dict[str, Any] = {}
    for mode in ("plain", "plan"):
        subset = [r for r in rows if r["mode"] == mode]
        by_mode[mode] = {
            metric: {
                "passed": sum(bool(r["metrics"][metric]) for r in subset),
                "total": len(subset),
            }
            for metric in metrics
            if metric != "plan_usage"
        }
        by_mode[mode]["plan_usage"] = {
            "calls": sum(bool(r["metrics"]["plan_usage"]) for r in subset),
            "runs": len(subset),
        }
        by_mode[mode]["step_completion"] = {
            "steps_completed": sum(
                row["metrics"]["step_completion"]["completed"] for row in subset
            ),
            "steps_total": sum(row["metrics"]["step_completion"]["total"] for row in subset),
        }
        by_mode[mode]["by_case"] = {
            case["case_id"]: {
                metric: sum(
                    bool(r["metrics"][metric]) for r in subset if r["case_id"] == case["case_id"]
                )
                for metric in metrics
                if metric != "plan_usage"
            }
            for case in CASES
        }
        by_mode[mode]["by_case"]["step_completion"] = {
            case["case_id"]: {
                "steps_completed": sum(
                    r["metrics"]["step_completion"]["completed"]
                    for r in subset
                    if r["case_id"] == case["case_id"]
                ),
                "steps_total": sum(
                    r["metrics"]["step_completion"]["total"]
                    for r in subset
                    if r["case_id"] == case["case_id"]
                ),
            }
            for case in CASES
        }
    return {
        "experiment_id": experiment_id,
        "agent_scope_version": "2.0.8",
        "model": "qwen-plus",
        "temperature": 0,
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "run_count": len(rows),
        "mode": by_mode,
    }


def reevaluate_experiment(experiment_id: str) -> dict[str, Any]:
    """保留原始调用记录，另存使用严格契约判分的离线复核结果。"""

    reviewed_rows: list[dict[str, Any]] = []
    for mode in ("plain", "plan"):
        source = OUTPUT / f"task012c_{experiment_id}_{mode}.jsonl"
        rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        reviewed = []
        for row in rows:
            scored = {**row, "initial_metrics": row.get("metrics"), "metrics": review_record(row)}
            scored["score_revision"] = "authority-scope-step-contract-v2"
            reviewed.append(scored)
            reviewed_rows.append(scored)
        target = OUTPUT / f"task012c_{experiment_id}_rescored_{mode}.jsonl"
        target.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in reviewed),
            encoding="utf-8",
        )
    summary = summarize(experiment_id, reviewed_rows)
    (OUTPUT / "task012c_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


async def native_tool_smoke() -> dict[str, Any]:
    state = AgentState()
    toolkit = Toolkit(tools=[TaskCreate(), TaskGet(), TaskList(), TaskUpdate()])
    schemas = await toolkit.get_tool_schemas()
    expected = {"TaskCreate", "TaskGet", "TaskList", "TaskUpdate"}
    if {schema["function"]["name"] for schema in schemas} != expected:
        raise RuntimeError("原生 Task Tools Toolkit 注册不完整。")
    for tool in toolkit.tool_groups[0].tools:
        if tool.name in expected and not tool.is_state_injected:
            raise RuntimeError(f"{tool.name} 未启用 AgentState 注入")
    calls = [
        ("TaskCreate", {"subject": "smoke", "description": "verify native task context"}),
        ("TaskUpdate", {"task_id": "1", "status": "in_progress"}),
        ("TaskList", {}),
        ("TaskGet", {"task_id": "1"}),
    ]
    for index, (name, args) in enumerate(calls):
        tool_call = ToolCallBlock(id=f"smoke-{index}", name=name, input=json.dumps(args))
        result = [item async for item in toolkit.call_tool(tool_call, state)][-1]
        if result.state == ToolResultState.ERROR:
            raise RuntimeError(f"原生 {name} 调用失败")
    return {
        "import": True,
        "toolkit_registration": sorted(expected),
        "agent_state_injection": True,
        "crud_smoke": [(task.id, task.state) for task in state.tasks_context.tasks],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--case", action="append")
    parser.add_argument("--smoke-only", action="store_true")
    parser.add_argument("--reevaluate-experiment-id")
    args = parser.parse_args()
    if args.reevaluate_experiment_id:
        summary = reevaluate_experiment(args.reevaluate_experiment_id)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    smoke = asyncio.run(native_tool_smoke())
    print(json.dumps({"native_tool_smoke": smoke}, ensure_ascii=False))
    if args.smoke_only:
        return
    experiment_id, rows = asyncio.run(
        run(args.repeats, args.timeout, set(args.case) if args.case else None)
    )
    print(json.dumps(summarize(experiment_id, rows), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
