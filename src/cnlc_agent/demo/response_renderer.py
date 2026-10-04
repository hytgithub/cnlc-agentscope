"""按不可变 ResponseEvidenceEnvelope 渲染用户可见的业务回复。"""

from cnlc_agent.domain.response_evidence import (
    ResponseEvidenceEnvelope,
    ResponseOperation,
    ResponseSourceType,
    ResponseStatus,
)

_STAGE_NAMES = {
    "DATA_DECODE": "数据解编",
    "PREPROCESS": "数据预处理",
    "INTERPRET": "智能处理",
    "REPORT": "报告生成",
}


def render_response_evidence(envelope: ResponseEvidenceEnvelope | None) -> str:
    """固定回复核心事实；缺少执行证据、Mock 或 Fixture 均不能伪装成真实成功。"""

    if envelope is None:
        return (
            "本轮未取得业务工具或应用结果，因此没有查询或修改业务数据，也不能声明操作成功。"
            "当前只处理单井测井解释业务；若要修改，请明确参数名称和值与任务范围；"
            "若当前没有任务，请先上传受支持的井资料。"
        )

    prefix: list[str] = []
    if envelope.well_name or envelope.well_id:
        prefix.append(f"井：{envelope.well_name or envelope.well_id}")
    if envelope.task_id:
        prefix.append(f"Task：{envelope.task_id}")
    if envelope.execution_id:
        prefix.append(f"Execution：{envelope.execution_id}")
    if envelope.scope is not None:
        prefix.append(f"范围：{envelope.scope.description}")
    if envelope.revision is not None:
        prefix.append(f"版本：第 {envelope.revision} 版")
    facts = "；".join(prefix)
    header = f"{facts}\n" if facts else ""
    stage = _STAGE_NAMES.get(envelope.confirmed_stage or "", "当前")

    if envelope.status == ResponseStatus.NEED_CLARIFICATION:
        body = envelope.clarification or envelope.result_summary or "请补充必要信息。"
    elif envelope.status == ResponseStatus.UNSUPPORTED:
        body = envelope.result_summary or "当前能力不支持该请求，未执行。"
    elif envelope.status == ResponseStatus.REJECTED:
        body = envelope.result_summary or "请求已拒绝，未执行。"
    elif envelope.status == ResponseStatus.QUEUED:
        if envelope.operation == ResponseOperation.STAGE_CONFIRM:
            body = f"已确认{stage}阶段；后续执行已进入队列，尚未完成，这不代表下一阶段已经完成。"
        else:
            body = "执行已进入队列，尚未完成。"
    elif envelope.status == ResponseStatus.RUNNING:
        waiting_confirmation = (
            envelope.execution_status is not None
            and envelope.execution_status.value == "WAITING_CONFIRMATION"
        )
        if envelope.operation == ResponseOperation.STAGE_CONFIRM and waiting_confirmation:
            body = f"已确认{stage}阶段；后续阶段已完成并等待确认。"
        elif envelope.operation == ResponseOperation.STAGE_CONFIRM:
            body = f"已确认{stage}阶段；后续执行正在进行，尚未完成。"
        elif waiting_confirmation:
            body = "当前阶段已完成，执行正在等待用户确认；后续阶段尚未完成。"
        else:
            body = "执行正在进行，尚未完成。"
    elif envelope.status == ResponseStatus.FAILED:
        detail = envelope.error_code or envelope.result_summary or "执行失败"
        execution_status = (
            envelope.execution_status.value if envelope.execution_status is not None else None
        )
        if execution_status == "BLOCKED":
            body = f"解释流程已停止：缺少资料。{detail}"
        elif execution_status == "REVIEW_REQUIRED":
            body = f"解释流程已进入人工复核。{detail}"
        elif envelope.operation in {
            ResponseOperation.START,
            ResponseOperation.MODIFY,
            ResponseOperation.FULL_RERUN,
        }:
            body = f"解释执行失败（{detail}），请检查任务状态后重试或联系管理员。"
        elif envelope.operation in {
            ResponseOperation.REPORT_READ,
            ResponseOperation.REPORT_GENERATION,
        }:
            body = f"报告操作未成功（{detail}），请检查任务状态后重试。"
        else:
            body = f"执行未成功（{detail}）。请检查任务状态后重试或联系管理员。"
    elif envelope.operation == ResponseOperation.STAGE_CONFIRM:
        if envelope.execution_status is not None and envelope.execution_status.value == "SUCCESS":
            if envelope.confirmed_stage == "REPORT":
                body = "已确认报告生成阶段；报告生成已完成。"
            else:
                body = f"已确认{stage}阶段；后续执行已经完成。"
            if envelope.confirmed_stage == "REPORT" and envelope.report_markdown:
                body += "\n\n" + envelope.report_markdown
        else:
            body = f"已确认{stage}阶段；这只表示确认已记录，不代表下一阶段已完成。"
    elif envelope.operation == ResponseOperation.REPORT_READ:
        body = envelope.report_markdown or "该版本没有可读取的报告正文。"
    elif envelope.operation == ResponseOperation.REPORT_GENERATION:
        body = envelope.report_markdown or "报告生成操作已完成，但没有可展示的报告正文。"
    elif envelope.operation in {ResponseOperation.MODIFY, ResponseOperation.FULL_RERUN}:
        body = "参数修改并重新解释已完成。"
    elif envelope.operation == ResponseOperation.START:
        body = "解释执行已完成。"
    elif envelope.operation == ResponseOperation.STATUS:
        body = envelope.result_summary or "已读取当前执行状态。"
    else:
        body = envelope.result_summary or "已取得业务结果。"

    if envelope.execution_status is not None and envelope.execution_status.value == "WARNING":
        body = "执行已完成，但存在 warning。\n\n" + body
    if envelope.source_type in {
        ResponseSourceType.FIXTURE,
        ResponseSourceType.MOCK,
        ResponseSourceType.MIXED,
    }:
        body = "【非真实业务结果】\n" + body
    elif envelope.source_type == ResponseSourceType.DERIVED:
        body = "【派生结果；来源见证据引用】\n" + body
    elif envelope.source_type == ResponseSourceType.UNKNOWN and envelope.evidence_refs:
        body = "【结果来源未核验】\n" + body
    return header + body
