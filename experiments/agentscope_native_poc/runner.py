"""可选 qwen-plus A/B 运行器；无 opt-in 或凭据时安全跳过。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from agentscope.agent import Agent
from agentscope.credential import DashScopeCredential
from agentscope.message import ToolCallBlock, ToolResultBlock, UserMsg
from agentscope.model import DashScopeChatModel

from .agent import ExperimentConfig, build_agent
from .state import MockState, ToolCallRecord

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = Path(__file__).resolve().parent / "cases.json"
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "native_poc"


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
) -> dict[str, Any]:
    """为单条场景创建干净 Fixture，执行一次对话并提取可审计结果。"""

    state = MockState.fixture()
    agent = build_agent(model=model, config=config, state=state)
    response = await agent.reply(UserMsg(name="user", content=case["input"]))
    calls = _actual_tool_calls(agent, state)
    passed, failure_reason = evaluate_case(case, calls)
    return {
        "case_id": case["case_id"],
        "input": case["input"],
        "config": config.value,
        "expected_action": case["expected_action"],
        "actual_tool_calls": calls,
        "arguments": [item["arguments"] for item in calls],
        "result": [item["result"] for item in calls],
        "final_response_summary": (response.get_text_content() or "")[:600],
        "pass": passed,
        "failure_reason": failure_reason,
        "token_usage": _usage_summary(response),
    }


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

    if expected == "preflight_modify_parameter":
        preflight_index = names.index(expected)
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

    if expected == "compare_result_versions":
        comparison = business_calls[names.index(expected)]["arguments"]
        left = comparison.get("left_execution_id")
        right = comparison.get("right_execution_id")
        if not left or not right or left == right:
            return False, "版本比较未提供两个不同的明确版本"

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


async def run_ab() -> dict[str, Any]:
    """在显式 opt-in 且凭据完整时，各运行 12 条 no_skill / with_skill。"""

    if os.getenv("CNLC_RUN_NATIVE_POC_MODEL") != "1":
        return {"status": "SKIP", "reason": "CNLC_RUN_NATIVE_POC_MODEL 未设为 1。"}

    credential, complete, model_name = _model_configuration()
    if not complete or credential is None or model_name != "qwen-plus":
        return {"status": "SKIP", "reason": "缺少完整的项目 qwen-plus 配置。"}

    cases = _load_cases()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summaries: dict[str, dict[str, int]] = {}
    all_records: dict[str, list[dict[str, Any]]] = {}
    model = _make_model(credential)
    try:
        for config in (ExperimentConfig.NO_SKILL, ExperimentConfig.WITH_SKILL):
            records = []
            for case in cases:
                records.append(await _run_case(case, config, model))
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
            output_path = OUTPUT_DIR / f"{config.value}.jsonl"
            output_path.write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
                encoding="utf-8",
            )
    finally:
        await model.client.close()

    summary = {
        "status": "COMPLETED",
        "model": "qwen-plus",
        "scenarios": len(cases),
        "results": summaries,
        "comparison": _compare_configs(all_records),
    }
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


def main() -> None:
    """命令行入口；缺少显式 opt-in 时只返回 SKIP。"""

    parser = argparse.ArgumentParser(description="运行 Task 012A qwen-plus A/B 实验")
    parser.add_argument(
        "--reevaluate",
        action="store_true",
        help="使用当前 cases 判分规则离线重评已有 JSONL，不调用模型。",
    )
    args = parser.parse_args()
    result = reevaluate_saved_runs() if args.reevaluate else asyncio.run(run_ab())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
