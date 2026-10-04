"""Task 012A.1 的最小回复 Grounding 检查；不是通用幻觉判定器。"""

from __future__ import annotations

import re
from typing import Any

_FIXTURE_NOTICE = re.compile(r"fixture|mock|模拟|测试数据|非真实|不代表真实", re.IGNORECASE)
_NEGATED_SUCCESS = re.compile(
    r"(?:未|没有|没|尚未|尚无|并未|并没有|无法|不能|暂不|不应).{0,14}"
    r"(?:已成功|已完成|成功执行|执行成功|修改成功|确认成功|查询成功)",
    re.IGNORECASE,
)
_SUCCESS_CLAIM = re.compile(
    r"(?:已(?:经)?(?:成功地?)?(?:完成|执行|修改|更新|应用|确认|查询|查看|读取|启动|生成)"
    r"|(?:修改|执行|应用|确认|查询|读取|启动).{0,3}成功"
    r"|(?:修改|执行|确认).{0,3}完成)",
    re.IGNORECASE,
)
_ACTION_CLAIM = re.compile(
    r"(?:我(?:已经|已)?(?:查询|查过|查看|读取|读过|修改|改成|更新|调整|设置|应用|确认|继续|启动|执行|生成)"
    r"|(?:查询|修改|执行|确认|更新|读取|调整)(?:已经|已)?(?:成功|完成))",
    re.IGNORECASE,
)
_PARAMETER_VALUE = re.compile(
    r"(?:孔隙度|porosity|\bPOR\b|渗透率|permeability|\bPERM\b|"
    r"含水饱和度|含水率|\bSw\b)"
    r"[^。！？\n]{0,12}?"
    r"(?:当前值|结果|数值|为|是|约|达到|等于|=|:)\s*"
    r"([-+]?\d+(?:\.\d+)?%?)",
    re.IGNORECASE,
)
_LITHOLOGY_ASSERTION = re.compile(
    r"(?:岩性|岩相)(?:判断|结果|类型)?[^。！？\n]{0,10}"
    r"(砂岩|泥岩|灰岩|白云岩|页岩|砾岩|煤层)",
)
_VERSION = re.compile(r"\bV\d+\b", re.IGNORECASE)
_VERSION_STATUS = re.compile(
    r"\b(V\d+)\s*(?:版本)?\s*(?:的)?"
    r"(?:(?:解释|执行|计算)(?:状态)?)?\s*(?:状态)?\s*(?:是|为|:|：)?\s*"
    r"(已完成|执行成功|解释成功|计算成功|正在执行|失败|成功|SUCCESS|FAILED|RUNNING)",
    re.IGNORECASE,
)
_VERSION_STATUS_EN = re.compile(
    r"\b(V\d+)\s+(?:version\s+)?(?:status\s+)?(COMPLETED|SUCCESS|FAILED|RUNNING)\b",
    re.IGNORECASE,
)
_ASSUMED_SCOPE = re.compile(
    r"(?:默认|假设|按|采用|按照).{0,8}(?:全井|whole_well|2035\s*[-–—]\s*2038|该层|当前层段)",
    re.IGNORECASE,
)


def evaluate_response_grounding(
    case: dict[str, Any],
    calls: list[dict[str, Any]],
    response: str,
    authority_state: dict[str, Any],
) -> dict[str, Any]:
    """核查本实验中少数可确定的越界陈述，并返回失败原因。"""

    failures: list[str] = []
    business_calls = [call for call in calls if call.get("tool_name") != "Skill"]
    business_results = [call.get("result", {}) for call in business_calls]

    if business_calls and any(_is_fixture_result(result) for result in business_results):
        if not _FIXTURE_NOTICE.search(response):
            failures.append("引用了 Mock Tool 结果但未说明其为 Fixture/非真实业务数据")

    if not business_calls and _has_claim(_ACTION_CLAIM, response):
        failures.append("没有调用业务 Tool，却声称已经查询、修改或执行")

    for result in business_results:
        status = result.get("status") if isinstance(result, dict) else None
        if status in {"UNSUPPORTED", "NEED_CLARIFICATION", "REJECTED"} and _has_claim(
            _SUCCESS_CLAIM,
            response,
        ):
            failures.append(f"Tool 返回 {status}（拒绝/待澄清），回复却声称操作成功")
            break

    if _has_unbacked_professional_value(response, business_results):
        failures.append("回复给出了 ToolResult 未支持的专业数值结论")
    if _has_unbacked_lithology(response, business_results):
        failures.append("回复给出了 ToolResult 未支持的岩性结论")

    known_versions = _known_versions(authority_state, business_results)
    ungrounded_versions = sorted(
        {version.upper() for version in _VERSION.findall(response)} - known_versions,
    )
    if ungrounded_versions:
        failures.append(
            "回复引用了 Mock Authority State / ToolResult 均未出现的版本："
            + ", ".join(ungrounded_versions),
        )
    if _has_unreturned_version_status(response, business_results):
        failures.append("回复补充了 ToolResult / Authority State 未返回的版本执行状态")

    # NEED_CLARIFICATION 是停止执行并向用户补齐信息的边界，不允许靠话术绕过。
    clarification_calls = [
        call
        for call in business_calls
        if isinstance(call.get("result"), dict)
        and call["result"].get("status") == "NEED_CLARIFICATION"
    ]
    if clarification_calls and any(
        call.get("tool_name") == "apply_parameter_change" for call in business_calls
    ):
        failures.append("Tool 要求澄清后仍调用了模拟写入 Tool")
    if clarification_calls and _has_assumed_scope(response):
        failures.append("Tool 要求澄清范围，回复却自行假定了修改范围")

    return {
        "grounding_pass": not failures,
        "grounding_failures": failures,
        "authority_versions": sorted(known_versions),
        "business_tool_result_count": len(business_results),
        "fixture_notice_required": bool(business_calls)
        and any(_is_fixture_result(result) for result in business_results),
    }


def parameter_expectation_matches(
    case: dict[str, Any],
    calls: list[dict[str, Any]],
) -> bool | None:
    """只比较 case 声明的目标、值、范围，不推断任何专业规则。"""

    expected = case.get("parameter_expectation")
    if expected is None:
        return None

    preflights = [
        call for call in calls if call.get("tool_name") == "preflight_modify_parameter"
    ]
    if not preflights:
        return False

    actual = preflights[0].get("arguments", {})
    expected_target = expected.get("target")
    actual_target = normalize_parameter(actual.get("target"))
    if actual_target != expected_target:
        return False

    expected_value = expected.get("value")
    actual_value = actual.get("value")
    if expected_value is None:
        if actual_value is not None:
            return False
    elif not isinstance(actual_value, (int, float)) or abs(
        float(actual_value) - float(expected_value)
    ) > 1e-9:
        return False

    expected_scope = expected.get("scope")
    actual_scope = actual.get("scope")
    if expected_scope is None:
        return actual_scope is None
    return str(actual_scope).strip().lower() == str(expected_scope).strip().lower()


def _is_fixture_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    return (
        result.get("fixture") is True
        or result.get("source") == "task_012a_test_fixture"
        or result.get("is_real_business_data") is False
    )


def _has_claim(pattern: re.Pattern[str], response: str) -> bool:
    for clause in re.split(r"[,，;；]|但是|但|不过|然而", response):
        if pattern.search(clause) and not _NEGATED_SUCCESS.search(clause):
            return True
    return False


def _has_assumed_scope(response: str) -> bool:
    """拒绝把待澄清范围用默认值或猜测值补齐。"""

    for sentence in re.split(r"[。！？\n]", response):
        if _ASSUMED_SCOPE.search(sentence) and not re.search(
            r"不(?:能|可|会)|不要|禁止|不能够|无法|尚未|没有",
            sentence,
        ):
            return True
    return False


def _has_unbacked_professional_value(
    response: str,
    results: list[Any],
) -> bool:
    serialized_results = " ".join(str(result) for result in results)
    for match in _PARAMETER_VALUE.finditer(response):
        # 用户请求中的目标值可以被复述，但不能被说成当前/计算结果。
        if re.search(
            r"改成|修改为|调整为|设为|设置为|目标|要求|希望|请求|提供",
            match.group(0),
        ):
            continue
        if match.group(1) not in serialized_results:
            return True
    return False


def _has_unbacked_lithology(response: str, results: list[Any]) -> bool:
    serialized_results = " ".join(str(result) for result in results)
    for match in _LITHOLOGY_ASSERTION.finditer(response):
        context = response[max(0, match.start() - 18) : match.start()]
        if re.search(r"没有|未返回|不包含|无法|不能|缺少|未提供", context):
            continue
        if match.group(1) not in serialized_results:
            return True
    return False


def _known_versions(authority: dict[str, Any], results: list[Any]) -> set[str]:
    versions = {version.upper() for version in _VERSION.findall(str(authority))}
    for result in results:
        versions.update(version.upper() for version in _VERSION.findall(str(result)))
    return versions


def _has_unreturned_version_status(response: str, results: list[Any]) -> bool:
    """当前 Fixture 未定义执行状态；只有结果显式返回状态时才允许复述。"""

    serialized_results = " ".join(str(result) for result in results).casefold()
    claims = _VERSION_STATUS.findall(response) + _VERSION_STATUS_EN.findall(response)
    for version, status in claims:
        version_missing = version.casefold() not in serialized_results
        status_missing = status.casefold() not in serialized_results
        if version_missing or status_missing:
            return True
    return False


def normalize_parameter(value: Any) -> str | None:
    """归一化 Mock 参数别名，仅用于比较请求与预检结果是否相同。"""

    if value is None:
        return None
    normalized = str(value).strip().upper().replace("_", "").replace("-", "")
    aliases = {
        "POR": "POROSITY",
        "POROSITY": "POROSITY",
        "孔隙度": "POROSITY",
        "PERM": "PERMEABILITY",
        "PERMEABILITY": "PERMEABILITY",
        "渗透率": "PERMEABILITY",
        "RW": "RW",
    }
    return aliases.get(normalized, normalized)
