"""可选 qwen-plus A/B 运行器；无 opt-in 或凭据时安全跳过。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentscope.agent import Agent
from agentscope.credential import DashScopeCredential
from agentscope.message import ToolCallBlock, ToolResultBlock, UserMsg
from agentscope.model import DashScopeChatModel

from .agent import ExperimentConfig, build_agent
from .grounding import (
    evaluate_response_grounding,
    normalize_parameter,
    parameter_expectation_matches,
)
from .state import MockState, ToolCallRecord

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = Path(__file__).resolve().parent / "cases.json"
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "native_poc"
DEFAULT_REPEAT_CASE_IDS = (
    "02",
    "03",
    "05",
    "06",
    "07",
    "09",
    "10",
    "13",
    "14",
    "15",
    "16",
)


def _model_configuration() -> tuple[DashScopeCredential | None, bool, str | None]:
    """从项目现有 ConnectionSettings 读取 qwen-plus 配置，不输出密钥。"""

    from cnlc_agent.config.settings import ConnectionSettings

    connections = ConnectionSettings()
    complete = bool(
        connections.model_api_key
        and connections.model_base_url
        and connections.model_name == "qwen-plus"
    )
    if not complete:
        return None, False, None
    credential = DashScopeCredential(
        name="Task 012A qwen-plus",
        api_key=connections.model_api_key,
        base_url=connections.model_base_url,
    )
    return credential, True, connections.model_name


def _make_model(credential: DashScopeCredential) -> DashScopeChatModel:
    """创建相同参数的 qwen-plus 客户端，固定 temperature 以减少随机波动。"""

    return DashScopeChatModel(
        credential=credential,
        model="qwen-plus",
        parameters=DashScopeChatModel.Parameters(
            temperature=0,
            parallel_tool_calls=False,
        ),
        stream=False,
        max_retries=1,
    )


def _load_cases() -> list[dict[str, Any]]:
    """加载固定场景集，避免 A/B 两组使用不同请求文本。"""

    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return payload["cases"]


async def _run_case(
    case: dict[str, Any],
    config: ExperimentConfig,
    model: DashScopeChatModel,
    experiment_id: str,
    run_index: int,
    repeat_count: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    """为一条 A/B 样本建立干净 Fixture，记录元信息、调用和最终回复。"""

    state = MockState.fixture()
    agent = build_agent(model=model, config=config, state=state)
    initial_authority = state.context()
    audit = await _agent_audit(agent, config)
    response = None
    model_error = None
    try:
        response = await asyncio.wait_for(
            agent.reply(UserMsg(name="user", content=case["input"])),
            timeout=timeout_seconds,
        )
    except Exception as error:  # noqa: BLE001 - 单条 API 失败要记证据并继续余下样本。
        model_error = {
            "type": type(error).__name__,
            "message": "qwen-plus 调用失败；为避免记录敏感请求细节，省略异常正文。",
        }
    calls = _actual_tool_calls(agent, state)
    response_text = response.get_text_content() or "" if response is not None else ""
    route_pass, route_failure = evaluate_case(case, calls)
    grounding = evaluate_response_grounding(
        case=case,
        calls=calls,
        response=response_text,
        authority_state={
            "initial": initial_authority,
            "final": state.context(),
        },
    )
    if model_error:
        route_pass = False
        route_failure = "模型调用失败；本样本没有可判定的完整回答。"
    parameter_correct = parameter_expectation_matches(case, calls)
    skill_calls = [call for call in calls if call["tool_name"] == "Skill"]
    skill_reads = _skill_read_audit(skill_calls, audit["loaded_skill_markdown"])
    clarification_correct = _clarification_correct(case, calls, response_text)
    route_failures = [route_failure] if route_failure else []
    overall_pass = route_pass and grounding["grounding_pass"]
    return {
        "run_id": f"{experiment_id}:{config.value}:{case['case_id']}:{run_index}",
        "experiment_id": experiment_id,
        "run_index": run_index,
        "repeat_count_for_case": repeat_count,
        "case_id": case["case_id"],
        "input": case["input"],
        "config": config.value,
        "config_detail": {
            "model": "qwen-plus",
            "temperature": 0,
            "parallel_tool_calls": False,
            "model_timeout_seconds": timeout_seconds,
            "base_system_prompt_sha256": audit["base_system_prompt_sha256"],
            "effective_system_prompt_sha256": audit["effective_system_prompt_sha256"],
            "business_tool_schema_sha256": audit["business_tool_schema_sha256"],
            "registered_skill_names": audit["registered_skill_names"],
        },
        "fixture_initial_state": initial_authority,
        "fixture_final_state": state.context(),
        "skill_registration": {
            "registered_skill_names": audit["registered_skill_names"],
            "loader_loaded_skill_body_names": audit["loader_loaded_skill_body_names"],
        },
        "skill_metadata_visible": audit["skill_metadata_visible"],
        "skillviewer_called": bool(skill_calls),
        "skillviewer_call_count": len(skill_calls),
        "skillviewer_skill_names": [call["arguments"].get("skill") for call in skill_calls],
        "skillviewer_steps": [call["sequence"] for call in skill_calls],
        "skill_body_returned_to_model": any(item["body_returned_to_model"] for item in skill_reads),
        "skill_read_audit": skill_reads,
        "business_tool_calls_after_skill": _business_calls_after_skill(calls),
        "tool_call_sequence": [call["tool_name"] for call in calls],
        "expected_action": case["expected_action"],
        "actual_tool_calls": calls,
        "arguments": [item["arguments"] for item in calls],
        "result": [item["result"] for item in calls],
        "final_response": response_text,
        "final_response_summary": response_text[:600],
        "route_pass": route_pass,
        "route_failures": route_failures,
        "grounding_pass": grounding["grounding_pass"],
        "grounding_failures": grounding["grounding_failures"],
        "grounding_evidence": grounding,
        "overall_pass": overall_pass,
        "pass": overall_pass,
        "failure_reason": "; ".join(route_failures + grounding["grounding_failures"]) or None,
        "parameter_correct": parameter_correct,
        "illegal_write_attempts": _illegal_write_attempts(case, calls),
        "unnecessary_full_interpretation": _unnecessary_full_interpretation(case, calls),
        "clarification_correct": clarification_correct,
        "token_usage": _usage_summary(response) if response is not None else None,
        "model_error": model_error,
    }


async def _agent_audit(agent: Agent, config: ExperimentConfig) -> dict[str, Any]:
    """记录 AgentScope 实际构造的 Skill 元信息及 A/B 公共配置摘要。"""

    toolkit = agent.toolkit
    activated = agent.state.tool_context.activated_groups
    available_skills = await toolkit._get_available_skills(activated)
    system_prompt = await agent._get_system_prompt()
    tool_schemas = await toolkit.get_tool_schemas()
    business_schemas = [
        schema for schema in tool_schemas if schema["function"]["name"] != "Skill"
    ]
    metadata_visible = []
    for skill in available_skills.values():
        if f"<name>{skill.name}</name>" in system_prompt:
            metadata_visible.append(
                {"name": skill.name, "description": skill.description},
            )
    return {
        "config": config.value,
        "registered_skill_names": sorted(available_skills),
        "loader_loaded_skill_body_names": sorted(available_skills),
        "loaded_skill_markdown": {
            skill.name: skill.markdown for skill in available_skills.values()
        },
        "skill_metadata_visible": metadata_visible,
        "base_system_prompt_sha256": _sha256(agent._system_prompt),
        "effective_system_prompt_sha256": _sha256(system_prompt),
        "business_tool_schema_sha256": _sha256(
            json.dumps(business_schemas, ensure_ascii=False, sort_keys=True),
        ),
    }


def _sha256(value: str) -> str:
    """生成配置证据的摘要，避免重复保存整份提示词和 Schema。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _actual_tool_calls(agent: Agent, state: MockState) -> list[dict[str, Any]]:
    """提取 AgentScope ToolCall 顺序，并关联 Mock Tool / SkillViewer 结果。"""

    records_by_name: dict[str, deque[ToolCallRecord]] = defaultdict(deque)
    for record in state.trace:
        records_by_name[record.tool_name].append(record)
    results_by_id: dict[str, ToolResultBlock] = {}
    for message in agent.state.context:
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                results_by_id[block.id] = block

    actual: list[dict[str, Any]] = []
    for message in agent.state.context:
        for block in message.content:
            if not isinstance(block, ToolCallBlock):
                continue
            arguments = _parse_tool_input(block.input)
            if block.name == "Skill":
                result_block = results_by_id.get(block.id)
                actual.append(
                    {
                        "sequence": len(actual) + 1,
                        "call_id": block.id,
                        "tool_name": block.name,
                        "arguments": arguments,
                        "result": _tool_result_content(result_block),
                    }
                )
                continue

            queue = records_by_name.get(block.name)
            if not queue:
                continue
            record = queue.popleft()
            actual.append(_record_to_dict(record, sequence=len(actual) + 1))

    # If the framework rejected a call before adding its ToolCallBlock to context,
    # retain the Mock-side contract record as evidence instead of hiding it.
    for queue in records_by_name.values():
        while queue:
            actual.append(_record_to_dict(queue.popleft(), sequence=len(actual) + 1))
    return actual


def _parse_tool_input(raw_input: str) -> dict[str, Any]:
    """解析 AgentScope ToolCall 参数，异常输入保留为空对象。"""

    try:
        value = json.loads(raw_input)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _tool_result_content(result: ToolResultBlock | None) -> Any:
    """保留 SkillViewer 的实际返回内容；它是公开 Skill 文本而非思维链。"""

    if result is None:
        return {"status": "MISSING_TOOL_RESULT"}
    if isinstance(result.output, str):
        return result.output
    return "\n".join(block.text for block in result.output if hasattr(block, "text"))


def _record_to_dict(record: ToolCallRecord, sequence: int) -> dict[str, Any]:
    """输出规定的 ToolCall 证据字段，不收集隐藏推理。"""

    return {
        "sequence": sequence,
        "call_id": record.call_id,
        "tool_name": record.tool_name,
        "arguments": record.arguments,
        "result": record.result,
    }


def _usage_summary(response: Any) -> dict[str, int] | None:
    """仅在 AgentScope 最终消息提供聚合 usage 时记录 token 计数。"""

    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is None or output_tokens is None:
        return None
    return {"input_tokens": int(input_tokens), "output_tokens": int(output_tokens)}


def _skill_read_audit(
    skill_calls: list[dict[str, Any]],
    loaded_markdown: dict[str, str],
) -> list[dict[str, Any]]:
    """只有 Viewer ToolResult 把正文送回对话，才记为模型实际读取 Skill。"""

    audit = []
    for call in skill_calls:
        name = call.get("arguments", {}).get("skill")
        body = call.get("result")
        expected_body = loaded_markdown.get(name)
        returned = isinstance(body, str) and expected_body is not None and body == expected_body
        audit.append(
            {
                "skill_name": name,
                "step": call["sequence"],
                "body_returned_to_model": returned,
                "body_sha256": _sha256(body) if isinstance(body, str) else None,
                "registered_body_sha256": _sha256(expected_body)
                if expected_body is not None
                else None,
                "body_character_count": len(body) if isinstance(body, str) else 0,
            },
        )
    return audit


def _business_calls_after_skill(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """列出每次 SkillViewer 后续紧接发生的领域 Tool，保留顺序。"""

    result = []
    for index, call in enumerate(calls):
        if call.get("tool_name") != "Skill":
            continue
        result.append(
            {
                "skill_name": call.get("arguments", {}).get("skill"),
                "skill_step": call["sequence"],
                "business_tools": [
                    next_call["tool_name"]
                    for next_call in calls[index + 1 :]
                    if next_call.get("tool_name") != "Skill"
                ],
            },
        )
    return result


def _clarification_correct(
    case: dict[str, Any],
    calls: list[dict[str, Any]],
    response: str,
) -> bool | None:
    """按测试预期确认必需澄清时是否停写并向用户索取缺失信息。"""

    if not case.get("clarification_required"):
        return None
    preflights = [call for call in calls if call["tool_name"] == "preflight_modify_parameter"]
    if not preflights:
        return False
    status = preflights[0].get("result", {}).get("status")
    asked = any(word in response for word in ("请", "补充", "说明", "明确", "告诉", "哪", "是否"))
    did_not_write = not any(
        call["tool_name"] == "apply_parameter_change" for call in calls
    )
    return status == "NEED_CLARIFICATION" and asked and did_not_write


def _illegal_write_attempts(
    case: dict[str, Any],
    calls: list[dict[str, Any]],
) -> int:
    """统计违反本实验预检或场景边界的 apply 调用尝试；Tool 会自行拒绝。"""

    allowed_by_case = (
        "apply_parameter_change" in case.get("allowed_tools", [])
        and not case.get("clarification_required", False)
    )
    approved: dict[str, Any] | None = None
    attempts = 0
    for call in calls:
        if call["tool_name"] == "preflight_modify_parameter":
            result = call.get("result", {})
            approved = result if result.get("status") == "ALLOWED" else None
        elif call["tool_name"] == "apply_parameter_change":
            requested = call.get("arguments", {})
            approved_value = approved.get("value") if approved else None
            requested_value = requested.get("value")
            matching_value = (
                isinstance(approved_value, (int, float))
                and isinstance(requested_value, (int, float))
                and abs(float(approved_value) - float(requested_value)) <= 1e-9
            )
            matching = (
                approved is not None
                and normalize_parameter(requested.get("target"))
                == normalize_parameter(approved.get("target"))
                and matching_value
                and _normalize_scope(requested.get("scope"))
                == _normalize_scope(approved.get("scope"))
            )
            if not allowed_by_case or not matching:
                attempts += 1
            approved = None
    return attempts


def _normalize_scope(value: Any) -> str | None:
    """统一 Mock 全井范围的别名，避免把同一预检错误记作非法写入。"""

    if value is None:
        return None
    normalized = str(value).strip().lower()
    return "whole_well" if normalized in {"whole_well", "well", "全井"} else normalized


def _unnecessary_full_interpretation(
    case: dict[str, Any],
    calls: list[dict[str, Any]],
) -> int:
    """统计非完整解释请求中误启动完整解释的次数。"""

    if case.get("expected_action") == "start_full_interpretation":
        return 0
    return sum(call["tool_name"] == "start_full_interpretation" for call in calls)


def evaluate_case(case: dict[str, Any], calls: list[dict[str, Any]]) -> tuple[bool, str | None]:
    """按场景的允许、禁止与预期动作判定，不按最终话术主观打分。"""

    skill_calls = [item for item in calls if item["tool_name"] == "Skill"]
    business_calls = [item for item in calls if item["tool_name"] != "Skill"]
    names = [item["tool_name"] for item in business_calls]
    allowed = set(case["allowed_tools"])
    forbidden = set(case["forbidden_tools"])
    unexpected = sorted(set(names) - allowed)
    forbidden_used = sorted(set(names) & forbidden)
    if unexpected:
        return False, f"调用了允许列表之外的 Tool: {', '.join(unexpected)}"
    if forbidden_used:
        return False, f"调用了禁止的 Tool: {', '.join(forbidden_used)}"

    expected = case["expected_action"]
    if expected == "NO_TOOL":
        if business_calls:
            return False, "领域外请求不应调用 Tool"
        if skill_calls:
            return False, "领域外请求不应读取测井业务 Skill"
        return True, None
    if expected not in names:
        return False, f"未调用预期 Tool: {expected}"

    required_sequence = case.get("required_tool_sequence", [])
    cursor = 0
    for expected_tool in required_sequence:
        try:
            cursor = names.index(expected_tool, cursor) + 1
        except ValueError:
            return False, f"业务 Tool 顺序缺少预期步骤: {expected_tool}"

    if "preflight_modify_parameter" in names:
        preflight_index = names.index("preflight_modify_parameter")
        preflight_status = business_calls[preflight_index]["result"].get("status")
        expected_statuses = case.get("expected_preflight_statuses", [])
        if expected_statuses and preflight_status not in expected_statuses:
            return False, (
                "预检状态不符合场景预期："
                f"{preflight_status or 'missing'} not in {expected_statuses}"
            )
        if "apply_parameter_change" in names:
            apply_index = names.index("apply_parameter_change")
            if preflight_index > apply_index:
                return False, "apply_parameter_change 出现在预检之前"
            if preflight_status != "ALLOWED":
                return False, "预检未返回 ALLOWED，却调用了 apply_parameter_change"
    elif case.get("expected_preflight_statuses"):
        return False, "场景要求修改预检，但未调用 preflight_modify_parameter"

    if expected == "compare_result_versions":
        comparison = business_calls[names.index(expected)]["arguments"]
        left = comparison.get("left_execution_id")
        right = comparison.get("right_execution_id")
        if not left or not right or left == right:
            return False, "版本比较未提供两个不同的明确版本"
        expected_comparison = case.get("expected_comparison")
        if expected_comparison and comparison != expected_comparison:
            return False, "版本比较没有使用预期的 Fixture 前后版本"

    expected_stage = case.get("expected_confirm_stage")
    if expected_stage is not None:
        confirm = next(call for call in business_calls if call["tool_name"] == "confirm_stage")
        if confirm.get("arguments", {}).get("stage") != expected_stage:
            return False, "确认阶段与上下文中的预期阶段不一致"

    return True, None


def reevaluate_saved_runs() -> dict[str, Any]:
    """按当前 cases 判分规则离线重算已有模型记录，不再次调用模型。"""

    cases_by_id = {item["case_id"]: item for item in _load_cases()}
    all_records: dict[str, list[dict[str, Any]]] = {}
    summaries: dict[str, dict[str, int]] = {}
    for config in (ExperimentConfig.NO_SKILL, ExperimentConfig.WITH_SKILL):
        output_path = OUTPUT_DIR / f"{config.value}.jsonl"
        records = [
            json.loads(line)
            for line in output_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for record in records:
            case = cases_by_id[record["case_id"]]
            passed, failure_reason = evaluate_case(case, record["actual_tool_calls"])
            record["pass"] = passed
            record["failure_reason"] = failure_reason
        all_records[config.value] = records
        summaries[config.value] = {
            "passed": sum(item["pass"] for item in records),
            "total": len(records),
            "skill_viewer_calls": sum(
                call["tool_name"] == "Skill"
                for item in records
                for call in item["actual_tool_calls"]
            ),
        }
        output_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
            encoding="utf-8",
        )

    summary = {
        "status": "REEVALUATED",
        "model": "qwen-plus",
        "scenarios": len(cases_by_id),
        "results": summaries,
        "comparison": _compare_configs(all_records),
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def reevaluate_latest_task012a1_run() -> dict[str, Any]:
    """用当前固定集和 evaluator 重算最新已完成的 012A.1 原始调用记录。"""

    summary_path = OUTPUT_DIR / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("task") != "012A.1" or summary.get("status") != "COMPLETED":
        raise ValueError("summary.json 中没有已完成的 Task 012A.1 实验。")

    cases_by_id = {case["case_id"]: case for case in _load_cases()}
    all_records: dict[str, list[dict[str, Any]]] = {"no_skill": [], "with_skill": []}
    for config in (ExperimentConfig.NO_SKILL, ExperimentConfig.WITH_SKILL):
        output_path = OUTPUT_DIR / summary["result_files"][config.value]
        records = [
            json.loads(line)
            for line in output_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for record in records:
            case = cases_by_id[record["case_id"]]
            calls = record["actual_tool_calls"]
            response = record.get("final_response", "")
            record.setdefault("config_detail", {}).setdefault("model_timeout_seconds", None)
            route_pass, route_failure = evaluate_case(case, calls)
            grounding = evaluate_response_grounding(
                case=case,
                calls=calls,
                response=response,
                authority_state={
                    "initial": record.get("fixture_initial_state", {}),
                    "final": record.get("fixture_final_state", {}),
                },
            )
            route_failures = [route_failure] if route_failure else []
            record.update(
                {
                    "skill_required": bool(case.get("skill_required")),
                    "route_pass": route_pass,
                    "route_failures": route_failures,
                    "grounding_pass": grounding["grounding_pass"],
                    "grounding_failures": grounding["grounding_failures"],
                    "grounding_evidence": grounding,
                    "overall_pass": route_pass and grounding["grounding_pass"],
                    "pass": route_pass and grounding["grounding_pass"],
                    "failure_reason": "; ".join(
                        route_failures + grounding["grounding_failures"],
                    )
                    or None,
                    "parameter_correct": parameter_expectation_matches(case, calls),
                    "illegal_write_attempts": _illegal_write_attempts(case, calls),
                    "unnecessary_full_interpretation": _unnecessary_full_interpretation(
                        case,
                        calls,
                    ),
                    "clarification_correct": _clarification_correct(case, calls, response),
                },
            )
        output_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
            encoding="utf-8",
        )
        all_records[config.value] = records

    summary["results"] = {
        config: _aggregate(records) for config, records in all_records.items()
    }
    summary["ab_fairness_all_pairs"] = all(
        all(pair["ab_fairness"].values()) for pair in _config_comparison(all_records)
    )
    summary["comparison"] = _config_comparison(all_records)
    summary["evaluation_revision"] = "recomputed_with_current_cases_and_grounding"
    summary.update(_timeout_summary(all_records))
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _rate(passed: int, total: int) -> float | None:
    """统一保留四位小数的比率；分母为零时明确返回 None。"""

    return round(passed / total, 4) if total else None


def _timeout_summary(all_records: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """从逐条配置恢复实际超时设置，续跑混合配置时不伪报单一值。"""

    counts: dict[float | None, int] = defaultdict(int)
    for records in all_records.values():
        for record in records:
            configured = record.get("config_detail", {}).get("model_timeout_seconds")
            counts[configured] += 1
    values = sorted(counts, key=lambda value: -1 if value is None else value)
    return {
        "model_timeout_seconds": values[0] if len(values) == 1 else None,
        "model_timeout_seconds_by_run": [
            {"seconds": value, "runs": counts[value]} for value in values
        ],
    }


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """对所有重复样本聚合路由、Skill、参数、安全和 Grounding 指标。"""

    total = len(records)
    skill_required = [item for item in records if item.get("skill_required")]
    parameter_items = [item for item in records if item.get("parameter_correct") is not None]
    clarification_items = [
        item for item in records if item.get("clarification_correct") is not None
    ]
    skill_calls = sum(item["skillviewer_called"] for item in records)
    route_pass = sum(item["route_pass"] for item in records)
    grounding_pass = sum(item["grounding_pass"] for item in records)
    overall_pass = sum(item["overall_pass"] for item in records)
    parameter_pass = sum(item["parameter_correct"] for item in parameter_items)
    clarification_pass = sum(item["clarification_correct"] for item in clarification_items)
    return {
        "runs": total,
        "route_pass": route_pass,
        "route_pass_rate": _rate(route_pass, total),
        "skillviewer_invocations": sum(item["skillviewer_call_count"] for item in records),
        "skillviewer_run_count": skill_calls,
        "skillviewer_call_rate": _rate(skill_calls, total),
        "skill_required_runs": len(skill_required),
        "skill_required_viewer_calls": sum(
            item["skillviewer_called"] for item in skill_required
        ),
        "skill_required_viewer_call_rate": _rate(
            sum(item["skillviewer_called"] for item in skill_required),
            len(skill_required),
        ),
        "parameter_correct": parameter_pass,
        "parameter_cases": len(parameter_items),
        "parameter_accuracy_rate": _rate(parameter_pass, len(parameter_items)),
        "illegal_write_attempts": sum(item["illegal_write_attempts"] for item in records),
        "unnecessary_full_interpretation_count": sum(
            item["unnecessary_full_interpretation"] for item in records
        ),
        "clarification_correct": clarification_pass,
        "clarification_cases": len(clarification_items),
        "clarification_correct_rate": _rate(
            clarification_pass,
            len(clarification_items),
        ),
        "grounding_pass": grounding_pass,
        "grounding_pass_rate": _rate(grounding_pass, total),
        "overall_pass": overall_pass,
        "overall_pass_rate": _rate(overall_pass, total),
        "token_usage_available_runs": sum(item["token_usage"] is not None for item in records),
    }


def _config_comparison(all_records: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """按用例和重复序号比较 A/B，而非只比较每个场景的最好一次。"""

    no_skill = {(item["case_id"], item["run_index"]): item for item in all_records["no_skill"]}
    with_skill = {
        (item["case_id"], item["run_index"]): item for item in all_records["with_skill"]
    }
    return [
        {
            "case_id": case_id,
            "run_index": run_index,
            "no_skill_route_pass": no_skill[(case_id, run_index)]["route_pass"],
            "with_skill_route_pass": with_skill[(case_id, run_index)]["route_pass"],
            "no_skill_grounding_pass": no_skill[(case_id, run_index)]["grounding_pass"],
            "with_skill_grounding_pass": with_skill[(case_id, run_index)]["grounding_pass"],
            "no_skill_overall_pass": no_skill[(case_id, run_index)]["overall_pass"],
            "with_skill_overall_pass": with_skill[(case_id, run_index)]["overall_pass"],
            "no_skill_tools": no_skill[(case_id, run_index)]["tool_call_sequence"],
            "with_skill_tools": with_skill[(case_id, run_index)]["tool_call_sequence"],
            "with_skill_viewer_called": with_skill[(case_id, run_index)]["skillviewer_called"],
            "ab_fairness": {
                "same_input": no_skill[(case_id, run_index)]["input"]
                == with_skill[(case_id, run_index)]["input"],
                "same_base_prompt": no_skill[(case_id, run_index)]["config_detail"][
                    "base_system_prompt_sha256"
                ]
                == with_skill[(case_id, run_index)]["config_detail"][
                    "base_system_prompt_sha256"
                ],
                "same_business_tool_schema": no_skill[(case_id, run_index)]["config_detail"][
                    "business_tool_schema_sha256"
                ]
                == with_skill[(case_id, run_index)]["config_detail"][
                    "business_tool_schema_sha256"
                ],
                "same_initial_fixture": no_skill[(case_id, run_index)][
                    "fixture_initial_state"
                ]
                == with_skill[(case_id, run_index)]["fixture_initial_state"],
            },
        }
        for case_id, run_index in no_skill
    ]


async def run_ab(
    repeat: int | None = None,
    repeat_case_ids: tuple[str, ...] | None = None,
    timeout_seconds: float | None = None,
    resume_experiment_id: str | None = None,
) -> dict[str, Any]:
    """按可配置重复次数运行同一固定集；目标重复集默认至少重复五次。"""

    if os.getenv("CNLC_RUN_NATIVE_POC_MODEL") != "1":
        return {"status": "SKIP", "reason": "CNLC_RUN_NATIVE_POC_MODEL 未设为 1。"}

    credential, complete, model_name = _model_configuration()
    if not complete or credential is None or model_name != "qwen-plus":
        return {"status": "SKIP", "reason": "缺少完整的项目 qwen-plus 配置。"}

    repeat = repeat if repeat is not None else int(os.getenv("CNLC_NATIVE_POC_REPEAT", "5"))
    if repeat < 1:
        raise ValueError("repeat 必须大于等于 1。")
    timeout_seconds = (
        timeout_seconds
        if timeout_seconds is not None
        else float(os.getenv("CNLC_NATIVE_POC_MODEL_TIMEOUT_SECONDS", "120"))
    )
    if timeout_seconds <= 0:
        raise ValueError("模型单样本超时必须大于 0 秒。")
    selected_repeat_ids = set(
        DEFAULT_REPEAT_CASE_IDS if repeat_case_ids is None else repeat_case_ids
    )
    cases = _load_cases()
    unknown_ids = selected_repeat_ids - {case["case_id"] for case in cases}
    if unknown_ids:
        raise ValueError(f"重复集包含未知 case_id: {', '.join(sorted(unknown_ids))}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    experiment_id = resume_experiment_id or (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    )
    paths = {
        config.value: OUTPUT_DIR / f"task012a1_{experiment_id}_{config.value}.jsonl"
        for config in (ExperimentConfig.NO_SKILL, ExperimentConfig.WITH_SKILL)
    }
    all_records: dict[str, list[dict[str, Any]]] = {"no_skill": [], "with_skill": []}
    if resume_experiment_id:
        for config_name, path in paths.items():
            if not path.is_file():
                raise ValueError(f"找不到可续跑的实验记录：{path.name}")
            all_records[config_name] = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
    existing_keys = {
        (record["config"], record["case_id"], record["run_index"])
        for records in all_records.values()
        for record in records
    }
    model = _make_model(credential)
    completed = 0
    planned_keys = {
        (config.value, case["case_id"], run_index)
        for case in cases
        for run_index in range(
            1,
            (repeat if case["case_id"] in selected_repeat_ids else 1) + 1,
        )
        for config in (ExperimentConfig.NO_SKILL, ExperimentConfig.WITH_SKILL)
    }
    total_runs = len(planned_keys - existing_keys)
    try:
        file_mode = "a" if resume_experiment_id else "x"
        with paths["no_skill"].open(file_mode, encoding="utf-8") as no_skill_file, paths[
            "with_skill"
        ].open(file_mode, encoding="utf-8") as with_skill_file:
            output_files = {"no_skill": no_skill_file, "with_skill": with_skill_file}
            for case in cases:
                case_repeat = repeat if case["case_id"] in selected_repeat_ids else 1
                for run_index in range(1, case_repeat + 1):
                    configs = [ExperimentConfig.NO_SKILL, ExperimentConfig.WITH_SKILL]
                    if run_index % 2 == 0:
                        configs.reverse()
                    for config in configs:
                        sample_key = (config.value, case["case_id"], run_index)
                        if sample_key in existing_keys:
                            continue
                        record = await _run_case(
                            case=case,
                            config=config,
                            model=model,
                            experiment_id=experiment_id,
                            run_index=run_index,
                            repeat_count=case_repeat,
                            timeout_seconds=timeout_seconds,
                        )
                        record["skill_required"] = bool(case.get("skill_required"))
                        all_records[config.value].append(record)
                        existing_keys.add(sample_key)
                        output_files[config.value].write(
                            json.dumps(record, ensure_ascii=False) + "\n",
                        )
                        output_files[config.value].flush()
                        completed += 1
                        print(
                            f"[{completed}/{total_runs}] {config.value} case={case['case_id']} "
                            f"run={run_index}/{case_repeat} route={record['route_pass']} "
                            f"grounding={record['grounding_pass']}",
                            flush=True,
                        )
    finally:
        await model.client.close()

    previous_summary = json.loads((OUTPUT_DIR / "summary.json").read_text(encoding="utf-8")) \
        if (OUTPUT_DIR / "summary.json").exists() else None
    if previous_summary and previous_summary.get("task") == "012A.1":
        previous_012a_summary = previous_summary.get("previous_task_012a_summary")
    else:
        previous_012a_summary = previous_summary
    summary = {
        "status": "COMPLETED",
        "task": "012A.1",
        "experiment_id": experiment_id,
        "model": "qwen-plus",
        "repeat": repeat,
        "repeated_case_ids": sorted(selected_repeat_ids),
        "single_run_case_ids": sorted(
            case["case_id"] for case in cases if case["case_id"] not in selected_repeat_ids
        ),
        "scenario_definitions": len(cases),
        "runs_per_config": len(all_records["no_skill"]),
        "result_files": {key: path.name for key, path in paths.items()},
        "results": {key: _aggregate(value) for key, value in all_records.items()},
        "ab_fairness_all_pairs": all(
            all(pair["ab_fairness"].values()) for pair in _config_comparison(all_records)
        ),
        "comparison": _config_comparison(all_records),
        "previous_task_012a_summary": previous_012a_summary,
        "skill_observability_definition": {
            "registered": "Toolkit 可枚举 Skill 名称",
            "metadata_visible": "Agent 的 system prompt 含 Skill name/description 元信息",
            "viewer_called": "真实模型产生 Skill ToolCallBlock",
            "body_returned_to_model": "Skill ToolResult 正文与对应已注册 Skill Markdown 完全匹配",
        },
    }
    summary.update(_timeout_summary(all_records))
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _compare_configs(
    all_records: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """按 case_id 并排比较两组 pass 与工具序列。"""

    no_skill = {item["case_id"]: item for item in all_records["no_skill"]}
    with_skill = {item["case_id"]: item for item in all_records["with_skill"]}
    return [
        {
            "case_id": case_id,
            "no_skill_pass": no_skill[case_id]["pass"],
            "with_skill_pass": with_skill[case_id]["pass"],
            "no_skill_tools": [
                item["tool_name"] for item in no_skill[case_id]["actual_tool_calls"]
            ],
            "with_skill_tools": [
                item["tool_name"] for item in with_skill[case_id]["actual_tool_calls"]
            ],
        }
        for case_id in no_skill
    ]


def _parse_case_ids(raw: str | None) -> tuple[str, ...] | None:
    """解析 CLI 覆盖的重复场景 ID；空字符串表示所有场景各运行一次。"""

    if raw is None:
        return None
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def main() -> None:
    """命令行入口；缺少显式 opt-in 时只返回 SKIP。"""

    parser = argparse.ArgumentParser(
        description="运行 Task 012A.1 qwen-plus Skill/Grounding A/B 实验",
    )
    parser.add_argument(
        "--reevaluate",
        action="store_true",
        help="使用当前 cases 判分规则离线重评已有 JSONL，不调用模型。",
    )
    parser.add_argument(
        "--reevaluate-task012a1",
        action="store_true",
        help="使用当前 case 与 Grounding 规则重评最近一次 012A.1 原始运行，不调用模型。",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=int(os.getenv("CNLC_NATIVE_POC_REPEAT", "5")),
        help="指定重复场景的运行次数；默认 5，可用环境变量 CNLC_NATIVE_POC_REPEAT 覆盖。",
    )
    parser.add_argument(
        "--repeat-case-ids",
        default=os.getenv("CNLC_NATIVE_POC_REPEAT_CASE_IDS"),
        help="逗号分隔的重复场景 ID；缺省使用稳定性测试集。",
    )
    parser.add_argument(
        "--model-timeout-seconds",
        type=float,
        default=float(os.getenv("CNLC_NATIVE_POC_MODEL_TIMEOUT_SECONDS", "120")),
        help="单个 case 的模型调用超时；可用 CNLC_NATIVE_POC_MODEL_TIMEOUT_SECONDS 覆盖。",
    )
    parser.add_argument(
        "--resume-experiment-id",
        help="从 artifacts/native_poc 下该 experiment_id 的两份 JSONL 续跑缺失样本。",
    )
    args = parser.parse_args()
    if args.reevaluate_task012a1:
        result = reevaluate_latest_task012a1_run()
    elif args.reevaluate:
        result = reevaluate_saved_runs()
    else:
        result = asyncio.run(
            run_ab(
                repeat=args.repeat,
                repeat_case_ids=_parse_case_ids(args.repeat_case_ids),
                timeout_seconds=args.model_timeout_seconds,
                resume_experiment_id=args.resume_experiment_id,
            ),
        )
    if result.get("task") == "012A.1":
        display = {
            "status": result["status"],
            "task": result["task"],
            "experiment_id": result["experiment_id"],
            "runs_per_config": result["runs_per_config"],
            "ab_fairness_all_pairs": result["ab_fairness_all_pairs"],
            "result_files": result["result_files"],
            "results": result["results"],
        }
    else:
        display = result
    print(json.dumps(display, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
