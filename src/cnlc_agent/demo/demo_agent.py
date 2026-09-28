"""现有测井解释应用服务的 AgentScope 对话外壳。"""

import builtins
import json
import re
from collections.abc import AsyncGenerator
from typing import Any, Literal
from uuid import uuid4

from agentscope.agent import Agent, ContextConfig, ModelConfig, ReActConfig
from agentscope.credential import CredentialBase, CredentialFactory
from agentscope.event import ToolResultEndEvent
from agentscope.formatter import DashScopeChatFormatter
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock, ToolResultState
from agentscope.middleware import MiddlewareBase, TracingMiddleware
from agentscope.model import ChatModelBase, ChatResponse, ModelCard, StructuredResponse
from agentscope.state import AgentState
from agentscope.tool import ToolChoice, Toolkit
from agentscope.workspace import Offloader
from pydantic import BaseModel, ConfigDict

from cnlc_agent.config.settings import AppSettings
from cnlc_agent.demo.execution_stream import (
    ExecutionReplyStreamer,
    ExecutionStreamingMiddleware,
)
from cnlc_agent.demo.interaction_middleware import InteractionStateMiddleware
from cnlc_agent.demo.interaction_state import render_interaction_result
from cnlc_agent.demo.tools import RUN_TOOL_NAME, RunWellInterpretationTool
from cnlc_agent.demo.upload_reply import UploadInterpretationReply

_STATUS_PATTERNS = (
    re.compile(r"(?:执行|处理|进行)?到(?:哪里|哪儿|哪一步|哪|什么地方)"),
    re.compile(r"(?:查看|当前)?进度|进度(?:怎么样|如何)"),
    re.compile(r"(?:执行|当前)状态"),
)
_WELL_ID_PATTERN = re.compile(r"\b(WELL_[A-Za-z0-9_-]+)\b", re.IGNORECASE)
_PREVIOUS_TASK_PHRASES = ("上一口井", "之前那口井", "前一口井")

_SUMMARY_TASK_CONTEXT_PATTERN = re.compile(
    r"<cnlc-task-context>(?P<payload>\{.*?\})</cnlc-task-context>",
    re.DOTALL,
)
_SUMMARY_FIELDS = {
    "task_overview",
    "current_state",
    "important_discoveries",
    "next_steps",
    "context_to_preserve",
}

DEMO_SYSTEM_PROMPT = """你是单井常规测井解释交互入口。
AgentScope ReAct 只理解用户意图，服务器解析、校验和执行。

只有两个工具：run_well_interpretation 用于明确的 Fixture 井号首次解释；
已有任务所有后续操作
必须使用 interpret_interpretation_operation。
测井解释范围内的条件请求、能力询问和不支持操作也必须调用该工具，
由服务器返回受控结论，不能直接用自然语言回答。
上传文件由 Upload Middleware 处理，无附件且无明确井号时请用户上传资料。

每轮只提交一个 Tool Call；
多个意图必须放在同一 PLAN 的 operations，先整体校验再执行。

不得直接回答专业结果，不计算专业参数，不决定 W01-W10、start_step 或 RUN/REUSE。

不要编造 task_id、execution_id、interval_id，不从聊天记忆猜当前井。
省略引用由服务器处理；

明确井号用 WELL_ID，上一口井用 PREVIOUS_TASK，上一版用 PREVIOUS，明确当前版本用 TASK_CURRENT。

查看报告只更新 View，不切 Active。
显式切井用 SET_ACTIVE_CONTEXT；
查看后隐式写冲突必须澄清。

PLAN 接受 PartialOperationPlan。
明确执行用 EXECUTION_REQUEST，查询用 READ_REQUEST，
问能力用 CAPABILITY_QUERY；
通常 persist_mode=CREATE_VERSION。
缺目标或值必须保留缺槽，不猜。

“孔隙度改成0.16” -> MODIFY_PARAMETER / POROSITY / ABSOLUTE 0.16 / unit=1。
PLAN 节点的数值必须放在 parameters.value，不能放在节点顶层。
例如 request={"mode":"PLAN","plan":{"input_classification":"EXECUTION_REQUEST",
"persist_mode":"CREATE_VERSION","original_instruction":"孔隙度改成0.16",
"operations":[{"operation_id":"op1","action":"MODIFY_PARAMETER","target":"POROSITY",
"parameters":{"value":{"mode":"ABSOLUTE","value":0.16,"unit":"1"}}}]}}。

“孔隙度改成16%” -> ABSOLUTE 0.16 / unit=1；
“孔隙度提高2%” -> PERCENT_CHANGE 2 / unit=%，不转绝对值。

“孔隙度、渗透率都改成0.16” -> 同一 PLAN 两节点 POROSITY、PERMEABILITY，不能拆成两个 Tool Call。

“改成0.17” -> MODIFY_PARAMETER、value=0.17、target=null；
服务器保存澄清。

“帮我改一下” -> MODIFY_PARAMETER、target=null、value=null；
下一轮“孔隙度”只补 target；如果仍缺值，再下一轮“0.16”只补 ABSOLUTE value=0.16。
每轮 CLARIFICATION_REPLY 只提交当前用户明确补充的槽位，不重建历史计划。

下一轮“孔隙度”且可信快照有 pending_operation_clarification，
使用 CLARIFICATION_REPLY patch target=POROSITY。

没有 Pending 不从旧聊天恢复修改值。
“不对，是第6层”且有 Pending -> CLARIFICATION_REPLY，
patch input_classification=CORRECTION、scope={kind:INTERVAL_ORDINAL,ordinal:6}；
已执行则提交新的 PLAN。

“算了” -> CANCEL；
“切到WELL_A” -> SET_ACTIVE_CONTEXT、task_reference WELL_ID。

“第5层孔隙度改成0.16” -> INTERVAL_ORDINAL ordinal=5；
“第3、5、7层” -> MULTI_INTERVAL_ORDINAL ordinals=[3,5,7]。

不得忽略局部范围，不能提供 interval_id。
当前不支持局部执行，由服务器安全拒绝。

“上一版报告” -> REPORT / REPORT / PREVIOUS；
“当前报告” -> REPORT / TASK_CURRENT；
状态 -> STATUS / WELL / TASK_CURRENT。

“全部重跑” -> FULL_RERUN / WELL；
“只重新算Sw” -> RECALCULATE / WATER_SATURATION，不能替换为全量重跑。

“你能只算Sw吗” -> CAPABILITY_QUERY，不能执行。
conditions 是 plan 顶层列表，每项只含 expression 和 operation_ids。
例如 conditions=[{"expression":"Sw大于60%","operation_ids":["op1"]}]；
对应 op1 使用 MODIFY_RESULT / ZONE_CLASSIFICATION；“水层”等非数值要求保留在
original_instruction，不能放入只接受数字的 parameters.value。该条件计划由服务器整体拒绝。
完整条件请求示例：{"request":{"mode":"PLAN","plan":{
"input_classification":"EXECUTION_REQUEST","persist_mode":"CREATE_VERSION",
"original_instruction":"如果Sw大于60%就改成水层",
"operations":[{"operation_id":"op1","action":"MODIFY_RESULT",
"target":"ZONE_CLASSIFICATION"}],
"conditions":[{"expression":"Sw大于60%","operation_ids":["op1"]}]}}}。
Tool 参数顶层只能有 request，不能添加 tool_call_id 等字段。
task_reference、execution_reference、scope 都是 operations[] 节点的直接字段，
与 action、target、parameters 同级；parameters 里不能放任何 reference 或 scope。
上一版报告完整示例：{"request":{"mode":"PLAN","plan":{
"input_classification":"READ_REQUEST","persist_mode":"CREATE_VERSION",
"original_instruction":"给我上一版报告","operations":[{"operation_id":"op1",
"action":"REPORT","target":"REPORT","execution_reference":{"kind":"PREVIOUS"}}]}}}。
“如果Sw大于60%就改成水层” -> conditions 加 MODIFY_RESULT / ZONE_CLASSIFICATION，不能忽略条件。

修改参数后要报告不另加 REPORT 节点，已有流式过程会返回该次报告。
修改加全量重跑等真实复合动作必须全部表达。

工具返回澄清、能力事实、拒绝或读取结果后结束本轮，禁止重试或改用其他工具。

开始、修改、重跑后的 W01-W10 和报告由服务器流式展示，不自己复述或编造专业结论。

天气、通用编程、笑话不调用工具，只简短说明当前 Agent 只处理单井常规测井解释业务。

不要泄露原始异常、凭据、内部 URL、数据库配置或本地路径。

"""


class MockTaskShellCredential(CredentialBase):
    """只在 Mock 联调环境发布 qwen-plus 外观，不保存密钥或访问公网。"""

    model_config = ConfigDict(title="CNLC Mock Shell")
    type: Literal["cnlc_mock_shell_credential"] = "cnlc_mock_shell_credential"

    @classmethod
    def get_chat_model_class(cls) -> builtins.type[ChatModelBase]:
        """让 AgentScope 会话装配和自动命名都使用确定性本地模型。"""

        return MockTaskShellModel


class MockTaskShellModel(ChatModelBase):
    """Mock 环境的确定性 ReAct 外壳，供真实 Web 联调时免公网模型运行。

    它只识别 Demo 已公开的任务级意图，任务引用由 SessionTaskResolver 解析；
    专业计算仍由 Workflow、专业 Tool 和 MockModelGateway 完成。
    """

    def __init__(
        self,
        credential: CredentialBase | None = None,
        model: str = "qwen-plus",
        parameters: ChatModelBase.Parameters | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            credential=credential or MockTaskShellCredential(name="CNLC Mock Shell"),
            model=model,
            parameters=parameters or self.Parameters(),
            stream=False,
            # 与发布给 AgentScope Web 的 ModelCard 保持一致，避免 32K 默认值让
            # 正常的长报告会话过早触发上下文压缩。
            context_size=kwargs.pop("context_size", 131_072),
            **kwargs,
        )
        self.formatter = DashScopeChatFormatter()

    @classmethod
    def list_models(cls, custom_yaml_dir: str | None = None) -> list[ModelCard]:
        """向官方 Web 模型选择器暴露唯一受支持的 qwen-plus 标识。"""

        del custom_yaml_dir
        return [
            ModelCard(
                name="qwen-plus",
                label="qwen-plus",
                status="active",
                context_size=131_072,
                output_size=8_192,
                parameter_schema=cls.Parameters.model_json_schema(),
                parameters_overrides={},
            ),
        ]

    async def generate_structured_output(
        self,
        messages: list[Msg],
        structured_model: type[BaseModel] | dict[Any, Any],
        **kwargs: Any,
    ) -> StructuredResponse:
        """本地完成标题或上下文摘要，并严格满足上游结构化输出契约。"""

        del kwargs
        schema = (
            structured_model.model_json_schema()
            if isinstance(structured_model, type) and issubclass(structured_model, BaseModel)
            else structured_model
        )
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        candidates = [message.get_text_content() or "" for message in reversed(messages)]
        text = next(
            (candidate.strip() for candidate in candidates if candidate.strip()),
            "CNLC 测井解释",
        )
        if _SUMMARY_FIELDS.issubset(properties):
            context = self._summary_task_context(messages)
            marker = (
                "<cnlc-task-context>"
                f"{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
                "</cnlc-task-context>"
                if context
                else "尚无已绑定的解释任务。"
            )
            return StructuredResponse(
                content={
                    "task_overview": "在当前会话中完成单井常规测井解释及任务级操作。",
                    "current_state": f"最近可信的持久化任务上下文：{marker}",
                    "important_discoveries": (
                        "任务标识只来自 Tool Result；专业结果由 Workflow 和专业 Tool 生成。"
                    ),
                    "next_steps": "等待用户继续查询状态、查看报告、修改参数或全量重跑。",
                    "context_to_preserve": (
                        "保持 W01-W10 顺序；只支持 sampling_interval、por、perm、"
                        "prediction_model 参数修改。"
                    ),
                }
            )
        title = "CNLC 测井解释" if "title" in text.lower() else text[:80]
        return StructuredResponse(content={"title": title})

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict[Any, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """根据受控中文指令选择任务级 Tool，不发起任何网络请求。"""

        del tools, tool_choice, kwargs
        if model_name != "qwen-plus":
            raise ValueError("Mock Demo 外壳只允许 qwen-plus 模型标识")
        last_user = max(
            (index for index, message in enumerate(messages) if message.role == "user"),
            default=-1,
        )
        instruction = (
            (messages[last_user].get_text_content() or "").strip() if last_user >= 0 else ""
        )
        current_results = [
            block
            for message in messages[last_user + 1 :]
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        if current_results:
            return ChatResponse(
                content=[TextBlock(text=self._render_result(current_results[-1]))],
                is_last=True,
            )
        snapshot = {}
        for message in messages:
            marker = "当前可信交互快照（不得由聊天记忆覆盖）：\n"
            text = message.get_text_content() or ""
            if message.role == "system" and marker in text:
                snapshot = json.loads(text.split(marker, 1)[1])
        name, parameters = self._command(instruction)
        pending = snapshot.get("pending_operation_clarification")
        target = {
            "孔隙度": "POROSITY",
            "por": "POROSITY",
            "渗透率": "PERMEABILITY",
            "perm": "PERMEABILITY",
        }.get(instruction.strip().lower())
        if pending and target:
            name, parameters = (
                "interpret_interpretation_operation",
                {"request": {"mode": "CLARIFICATION_REPLY", "patch": {"target": target}}},
            )
        elif pending and re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*%?", instruction):
            percent = instruction.endswith("%")
            raw_value = float(instruction.rstrip("%").strip())
            name, parameters = (
                "interpret_interpretation_operation",
                {
                    "request": {
                        "mode": "CLARIFICATION_REPLY",
                        "patch": {
                            "value": {
                                "mode": "ABSOLUTE",
                                "value": raw_value / (100 if percent else 1),
                                "unit": "1",
                            }
                        },
                    }
                },
            )
        elif pending and "不对" in instruction:
            ordinal = re.search(r"第(\d+)层", instruction)
            if ordinal:
                name, parameters = (
                    "interpret_interpretation_operation",
                    {
                        "request": {
                            "mode": "CLARIFICATION_REPLY",
                            "patch": {
                                "input_classification": "CORRECTION",
                                "scope": {
                                    "kind": "INTERVAL_ORDINAL",
                                    "ordinal": int(ordinal.group(1)),
                                },
                            },
                        }
                    },
                )
        if name is None:
            return ChatResponse(
                content=[
                    TextBlock(
                        text="请明确要修改的参数名称和值。"
                        if target
                        else "当前 Agent 只处理单井常规测井解释业务。"
                    )
                ],
                is_last=True,
            )
        return ChatResponse(
            content=[
                ToolCallBlock(
                    id=uuid4().hex,
                    name=name,
                    input=json.dumps(parameters, ensure_ascii=False),
                )
            ],
            is_last=True,
        )

    @staticmethod
    def _payload(block: ToolResultBlock) -> dict[str, Any]:
        """优先读取 Tool metadata，兼容只保留文本输出的历史消息。"""

        result = block.metadata.get("result")
        if isinstance(result, dict):
            return result
        output = block.output
        text = (
            output
            if isinstance(output, str)
            else next(
                (item.text for item in output if isinstance(item, TextBlock)),
                "{}",
            )
        )
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}

    @classmethod
    def _summary_contexts(cls, messages: list[Msg]) -> list[dict[str, Any]]:
        """只从官方摘要消息读取由本模型写入的任务上下文。"""

        for message in reversed(messages):
            text = message.get_text_content() or ""
            if not text.startswith("<system-info>Here is a summary of your previous work"):
                continue
            match = _SUMMARY_TASK_CONTEXT_PATTERN.search(text)
            if not match:
                continue
            try:
                payload = json.loads(match.group("payload"))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            candidates = payload.get("recent_tasks")
            if not isinstance(candidates, list):
                candidates = [payload]
            return [
                candidate
                for candidate in candidates
                if isinstance(candidate, dict)
                and isinstance(candidate.get("task_id"), str)
                and candidate["task_id"]
            ]
        return []

    @classmethod
    def _trusted_task_contexts(
        cls,
        messages: list[Msg],
        blocks: list[ToolResultBlock] | None = None,
    ) -> list[dict[str, Any]]:
        """按最近使用顺序保留不同井任务，仅信任 Tool 结果和受控摘要。"""

        results = blocks
        if results is None:
            results = [
                block
                for message in messages
                for block in message.content
                if isinstance(block, ToolResultBlock)
            ]
        contexts: list[dict[str, Any]] = []
        seen: set[str] = set()

        def append(payload: dict[str, Any]) -> None:
            task_id = payload.get("task_id")
            if not isinstance(task_id, str) or not task_id or task_id in seen:
                return
            context = {
                key: payload[key]
                for key in ("task_id", "execution_id", "well_id", "execution_sequence")
                if payload.get(key) is not None
            }
            contexts.append(context)
            seen.add(task_id)

        for block in reversed(results):
            append(cls._payload(block))
        for context in cls._summary_contexts(messages):
            append(context)
        return contexts

    @classmethod
    def _summary_task_context(cls, messages: list[Msg]) -> dict[str, Any]:
        """从 Tool 结果或既有受控摘要提取最小任务上下文，避免压缩后丢失绑定。"""

        contexts = cls._trusted_task_contexts(messages)
        if not contexts:
            return {}
        # 保留最近四口井，使长会话压缩后仍可返回上一口井的报告。
        return {**contexts[0], "recent_tasks": contexts[:4]}

    @staticmethod
    def _command(instruction: str) -> tuple[str | None, dict[str, Any]]:
        """仅 Mock 联调桩生成统一 Tool 请求；生产由 qwen-plus 读取同一 Schema。"""
        tool = "interpret_interpretation_operation"
        lower = instruction.lower()
        well = _WELL_ID_PATTERN.search(instruction)
        ref = None
        if any(word in instruction for word in _PREVIOUS_TASK_PHRASES):
            ref = {"kind": "PREVIOUS_TASK"}
        elif well:
            ref = {"kind": "WELL_ID", "value": well.group(1)}
        if any(word in instruction for word in ("算了", "取消")):
            return tool, {"request": {"mode": "CANCEL"}}
        if ref and any(word in instruction for word in ("切到", "切回", "接下来处理")):
            return tool, {"request": {"mode": "SET_ACTIVE_CONTEXT", "task_reference": ref}}
        if (
            well
            and any(word in instruction for word in ("开始", "解释"))
            and not any(word in instruction for word in ("重新", "改", "报告", "层"))
        ):
            return "run_well_interpretation", {"well_id": well.group(1)}
        plan: dict[str, Any] = {
            "input_classification": "EXECUTION_REQUEST",
            "persist_mode": "CREATE_VERSION",
            "original_instruction": instruction,
            "operations": [],
        }

        def add(action: str, target: str | None = "WELL", **fields: Any) -> None:
            op = {
                "operation_id": f"op{len(plan['operations']) + 1}",
                "action": action,
                "target": target,
                **fields,
            }
            if ref:
                op["task_reference"] = ref
            plan["operations"].append(op)

        layer = re.search(r"第([0-9、，,和\s]+)层", instruction)
        scope: dict[str, Any] = {"kind": "WHOLE_WELL"}
        if layer:
            ordinals = [int(n) for n in re.findall(r"\d+", layer.group(1))]
            scope = (
                {"kind": "INTERVAL_ORDINAL", "ordinal": ordinals[0]}
                if len(ordinals) == 1
                else {"kind": "MULTI_INTERVAL_ORDINAL", "ordinals": ordinals}
            )
        modify = any(
            word in lower for word in ("修改", "改成", "改为", "改一下", "提高", "降低")
        )
        full = any(
            word in lower
            for word in ("全部重跑", "全部重新跑", "全量重跑", "全流程重跑", "重新解释一次")
        )
        granular = any(
            word in lower for word in ("sw", "含水饱和度", "岩性", "层段", "第5层")
        ) and any(word in lower for word in ("算", "重新识别", "重新划分"))
        if "如果" in instruction:
            add("MODIFY_RESULT", "ZONE_CLASSIFICATION", scope=scope)
            plan["conditions"] = [{"expression": instruction, "operation_ids": ["op1"]}]
        elif granular:
            target = (
                "WATER_SATURATION"
                if "sw" in lower or "含水饱和度" in lower
                else "LITHOLOGY"
                if "岩性" in lower
                else "INTERVAL"
            )
            add("RECALCULATE", target, scope=scope)
            if any(word in lower for word in ("能", "可以")):
                plan["input_classification"] = "CAPABILITY_QUERY"
        elif modify:
            numeric = lower
            if well:
                numeric = numeric.replace(well.group(1).lower(), "")
            if layer:
                numeric = numeric.replace(layer.group(0).lower(), "")
            values = re.findall(r"(?:\d*\.\d+|\d+)%?", numeric)
            raw = values[-1] if values else None
            relative = any(word in lower for word in ("提高", "降低"))
            value = (
                None
                if raw is None
                else {
                    "mode": "PERCENT_CHANGE" if relative else "ABSOLUTE",
                    "value": float(raw.rstrip("%"))
                    / (100 if raw.endswith("%") and not relative else 1),
                    "unit": "%" if relative else "1",
                }
            )
            targets = [
                (words, target, name)
                for words, target, name in [
                    (("孔隙度", "por"), "POROSITY", None),
                    (("渗透率", "perm"), "PERMEABILITY", None),
                    (("采样间隔",), "WELL", "sampling_interval"),
                    (("rw", "archie"), "WELL", "rw"),
                    (("sw", "含水饱和度"), "WATER_SATURATION", "sw"),
                ]
                if any(word in lower for word in words)
            ]
            for _, target, name in targets or [((), "", None)]:
                params: dict[str, Any] = {"value": value}
                if name:
                    params["parameter_name"] = name
                add("MODIFY_PARAMETER", target or None, scope=scope, parameters=params)
            if full:
                add("FULL_RERUN")
            if "比较" in lower:
                add("COMPARE")
        elif full:
            add("FULL_RERUN", scope=scope)
        elif "报告" in instruction:
            kind = (
                "PREVIOUS"
                if "上一版" in instruction
                else "LATEST_SUCCESSFUL"
                if "成功" in instruction or (ref and ref["kind"] == "PREVIOUS_TASK")
                else "TASK_CURRENT"
                if "当前" in instruction
                else None
            )
            add("REPORT", "REPORT", **({"execution_reference": {"kind": kind}} if kind else {}))
            plan["input_classification"] = "READ_REQUEST"
        elif any(pattern.search(instruction) for pattern in _STATUS_PATTERNS) or any(
            word in instruction
            for word in ("现在呢", "为什么失败", "为什么停", "现在怎么了", "缺什么")
        ):
            add("STATUS", execution_reference={"kind": "TASK_CURRENT"})
            plan["input_classification"] = "READ_REQUEST"
        elif "什么是" in instruction or (
            "为什么" in instruction and any(word in instruction for word in ("层", "岩性"))
        ):
            add("EXPLAIN")
        elif "解释" in instruction:
            add("FULL_INTERPRET", scope=scope)
        else:
            return None, {}
        return tool, {"request": {"mode": "PLAN", "plan": plan}}

    @classmethod
    def _render_result(cls, block: ToolResultBlock) -> str:
        """回复只复述 Tool 的持久事实，不推断专业结果。"""

        payload = cls._payload(block)
        if payload.get("command") in {"START", "MODIFY", "FULL_RERUN"}:
            return (
                f"解释任务已提交：Execution #{payload.get('execution_sequence')}，"
                f"状态 {payload.get('execution_status')}。"
            )
        return render_interaction_result(payload)


# AgentScope 从持久化的 discriminator 还原凭证，因此必须在应用创建前注册。
CredentialFactory.register_credential(MockTaskShellCredential)


class LoggingInterpretationDemoAgent(Agent):
    """只暴露高层解释 Tool 的 AgentScope Agent，禁止绕过 Workflow 自行解释。"""

    async def _handle_error_tool_call(
        self, tool_call: ToolCallBlock, message: str, state: ToolResultState
    ) -> AsyncGenerator[Any, None]:
        """兼容 AgentScope 2.0.8 的前置校验错误出口，避免原始异常泄露或模型反复重试。

        on_acting 位于框架输入校验之后，无法拦截这一分支；只覆盖项目子类的
        错误投影，继续由上游保存消息和产生标准事件，不修改框架源码。
        """
        from cnlc_agent.demo.interaction_middleware import raw_operation_mode
        from cnlc_agent.demo.operation_tool import OPERATION_TOOL_NAME

        del message
        if tool_call.name == OPERATION_TOOL_NAME:
            pending = self._task_runner.pending_operation_clarification()
            mode = raw_operation_mode(tool_call.input)
            if pending is not None and mode not in {"PLAN", "CANCEL", "SET_ACTIVE_CONTEXT"}:
                # AgentScope 在 Middleware 之前拒绝 Schema 时，也不能销毁正在补齐的计划。
                self._task_runner.retain_operation_clarification()
            elif mode in {"PLAN", "CANCEL", "SET_ACTIVE_CONTEXT"}:
                self._task_runner.clear_operation_clarification()
        payload = {
            "error_code": "INVALID_OPERATION_PLAN",
            "message": "操作结构无效，请明确任务、目标、范围和修改值。",
        }
        async for event in super()._handle_error_tool_call(tool_call, payload["message"], state):
            if isinstance(event, ToolResultEndEvent):
                event.metadata.update(payload)
                for msg in self.state.context[-1:]:
                    for block in msg.content:
                        if isinstance(block, ToolResultBlock) and block.id == tool_call.id:
                            block.metadata.update(payload)
            yield event

    def __init__(
        self,
        name: str,
        system_prompt: str,
        model: ChatModelBase,
        toolkit: Toolkit | None = None,
        middlewares: list[MiddlewareBase] | None = None,
        state: AgentState | None = None,
        offloader: Offloader | None = None,
        model_config: ModelConfig | None = None,
        context_config: ContextConfig | None = None,
        react_config: ReActConfig | None = None,
        stream_step_delay_seconds: float | None = None,
        stream_report_chunk_delay_seconds: float | None = None,
        **kwargs: object,
    ) -> None:
        # 忽略前端自定义名称和提示词，保证 Demo 始终使用项目约束的系统提示。
        del name, system_prompt, kwargs
        if model.model != "qwen-plus":
            raise ValueError("AgentScope Demo Agent 只允许使用 qwen-plus")
        from cnlc_agent.demo.operation_tool import (
            ALLOWED_AGENT_TASK_TOOLS,
            InterpretInterpretationOperationTool,
        )
        from cnlc_agent.demo.task_tools import (
            TaskCommandRunner,
        )

        if toolkit and any(group.mcps or group.skills_or_loaders for group in toolkit.tool_groups):
            raise ValueError("Demo Agent 不允许注册 MCP 或技能工具")
        tools = [tool for group in (toolkit.tool_groups if toolkit else []) for tool in group.tools]
        names = [tool.name for tool in tools]
        if set(names) != ALLOWED_AGENT_TASK_TOOLS or len(names) != len(ALLOWED_AGENT_TASK_TOOLS):
            raise ValueError("Demo Agent 必须且只能注册预定义的任务级 Tool 集合")
        tool = next(tool for tool in tools if tool.name == RUN_TOOL_NAME)
        if not isinstance(tool, RunWellInterpretationTool) or any(
            not isinstance(item, InterpretInterpretationOperationTool)
            for item in tools
            if item is not tool
        ):
            raise ValueError("Demo Agent 需要受控任务 Tool 实现")
        runner = next(
            item.runner for item in tools if isinstance(item, InterpretInterpretationOperationTool)
        )
        if (
            not isinstance(runner, TaskCommandRunner)
            or any(
                item.runner is not runner
                for item in tools
                if isinstance(item, InterpretInterpretationOperationTool)
            )
            or tool._runner is not runner
        ):
            raise ValueError("Demo Agent 的任务 Tool 必须共享同一 TaskCommandRunner")
        self._task_runner = runner
        step_delay = (
            AppSettings().stream_step_delay_seconds
            if stream_step_delay_seconds is None
            else stream_step_delay_seconds
        )
        report_delay = (
            AppSettings().stream_report_chunk_delay_seconds
            if stream_report_chunk_delay_seconds is None
            else stream_report_chunk_delay_seconds
        )
        streamer = ExecutionReplyStreamer(
            runner.wait_for_execution_completion,
            runner.get_execution_report,
            step_delay_seconds=step_delay,
            report_chunk_delay_seconds=report_delay,
        )
        super().__init__(
            name="LoggingInterpretationDemoAgent",
            system_prompt=DEMO_SYSTEM_PROMPT,
            model=model,
            toolkit=Toolkit(tools=tools),
            middlewares=[
                # 框架级 Agent / Model / Tool Span 由 AgentScope 原生中间件负责。
                TracingMiddleware(),
                InteractionStateMiddleware(runner),
                UploadInterpretationReply(
                    tool,
                    streamer=streamer,
                ),
                ExecutionStreamingMiddleware(streamer),
                *(middlewares or []),
            ],
            state=state,
            offloader=offloader,
            model_config=model_config,
            context_config=context_config,
            react_config=react_config,
        )
        runner.attach_session_runtime_context(self.state.middle_context)
