"""现有测井解释应用服务的 AgentScope 对话外壳。"""

import builtins
import json
import re
from typing import Any, Literal
from uuid import uuid4

from agentscope.agent import Agent, ContextConfig, ModelConfig, ReActConfig
from agentscope.credential import CredentialBase, CredentialFactory
from agentscope.formatter import DashScopeChatFormatter
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.middleware import MiddlewareBase
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

DEMO_SYSTEM_PROMPT = """你是单井常规测井解释交互入口。ReAct 理解用户意图，Workflow 执行专业步骤。
必须实际调用工具执行任务操作，不得只输出调用计划、伪造成功或根据聊天记忆回答。
模型只可使用注册的五个业务工具与一个纯交互工具；不调用内部专业 Tool，不计算专业参数，
不决定 W01-W10、执行起点或 RUN/REUSE。专业结果只来自 Workflow。
任务统一用 task_reference；不要传 task_id。默认 CURRENT；上一口井 PREVIOUS_TASK；
明确井号 WELL_ID；不得构造 UUID，也不得把最近 ToolResult 当当前任务。
CURRENT/PREVIOUS_TASK 的 value 必须省略；WELL_ID/TASK_ID 必须带非空 value。
上传由接入层处理；明确 Fixture 井号时可 run_well_interpretation。
修改用 modify_well_interpretation，仅支持 sampling_interval、por、perm、prediction_model。
孔隙度16%可转 por=0.16；PERM 单位不擅自换算；prediction_model 不改变 qwen-plus。
“改成0.16”必须调用 request_interpretation_clarification(reason=PARAMETER_NAME, known_value=0.16)。
不猜参数。下一轮“孔隙度”且可信快照有 pending 时，必须实际调用
modify_well_interpretation(parameter_name="por")，省略 por 和其它值；工具从 pending 补齐。
没有 pending 时，只给参数名必须询问完整名称和值，不从旧聊天取值。
完整参数修改并要报告是一个 MODIFY。明确全部重跑用 rerun_well_interpretation。
同一句 MODIFY+FULL_RERUN 或混合不支持的操作，必须调用交互工具 reason=CONFLICT，不部分执行。
Rw、Archie m/n、Sw 等不支持参数必须调用交互工具 reason=UNSUPPORTED_PARAMETER。
重新计算含水饱和度、Sw-only、岩性-only、层段-only 必须调用交互工具 reason=UNSUPPORTED_OPERATION。
细层段原因、证据查询或专业概念问答用 reason=DOMAIN_QUERY；当前未开放完整专业问答。
这些都是测井领域请求，不能当 OUT_OF_DOMAIN，也不能自动 FULL_RERUN 或自己算 Sw。
查询进度、现在呢、为什么失败、缺什么，每轮必须调用 get_interpretation_status。
报告必须调用 get_interpretation_report。“上一版”是当前 Task 的 PREVIOUS Execution，不跨井；
“上一口井报告”是 PREVIOUS_TASK + LATEST_SUCCESSFUL；当前版 CURRENT，最近成功版 LATEST_SUCCESSFUL。
即使快照显示无任务、无上一版或无上一口井，也必须调用对应工具，由工具返回稳定错误。
工具返回澄清或拒绝后结束本轮，不重复尝试，不用旧报告冒充当前报告。
FAILED/BLOCKED/REVIEW_REQUIRED/WARNING 按持久事实解释，不自动改结论或继续步骤。
开始/修改/重跑成功后由系统流式返回过程和本轮报告。报告完整展示，不改写专业结论。
领域外的天气、通用编程、笑话不调用测井 Tool，只说明当前 Agent 的单井测井解释边界。
任何回复不暴露原始异常、API Key、内部 URL、数据库配置或本地路径。
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
        name, parameters = self._command(instruction)
        if name is None:
            # Mock 桩只解析紧邻轮次的受控快照，不从聊天记忆找旧值。
            names = {
                "孔隙度": "por",
                "por": "por",
                "渗透率": "perm",
                "perm": "perm",
                "采样间隔": "sampling_interval",
            }
            parameter = names.get(instruction.strip().lower())
            if parameter:
                pending = None
                for message in messages:
                    text = message.get_text_content() or ""
                    marker = "当前可信交互快照（不得由聊天记忆覆盖）：\n"
                    if message.role == "system" and marker in text:
                        snapshot = json.loads(text.split(marker, 1)[1])
                        pending = snapshot.get("pending_clarification")
                if pending:
                    name = "modify_well_interpretation"
                    parameters = {
                        "task_reference": {"kind": "TASK_ID", "value": pending["task_id"]},
                        "parameter_name": parameter,
                    }
                else:
                    return ChatResponse(
                        content=[TextBlock(text="请明确要修改的参数名称和值。")], is_last=True
                    )
            else:
                return ChatResponse(
                    content=[TextBlock(text="当前 Agent 只处理单井常规测井解释及相关任务操作。")],
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
        """把 Mock 联调指令映射到现有五个任务级 Tool。"""

        well_match = _WELL_ID_PATTERN.search(instruction)
        reference: dict[str, Any] = {"kind": "CURRENT"}
        if any(phrase in instruction for phrase in _PREVIOUS_TASK_PHRASES):
            reference = {"kind": "PREVIOUS_TASK"}
        elif well_match:
            reference = {"kind": "WELL_ID", "value": well_match.group(1)}
        task_reference = {"task_reference": reference}
        lower = instruction.lower()
        modify = any(word in lower for word in ("修改", "改成", "改为", "修改为"))
        full = any(word in lower for word in ("全部重跑", "全部重新跑", "全量重跑", "全流程重跑"))
        rerun = any(
            word in lower
            for word in ("重新计算", "重新算", "重算", "重新识别", "重新划分", "只重新算", "重跑")
        )
        granular = (
            not full
            and rerun
            and any(word in lower for word in ("sw", "含水饱和度", "岩性", "层段"))
        )
        interaction = "request_interpretation_clarification"
        if modify and (full or granular or "比较" in lower):
            return interaction, {"reason": "CONFLICT"}
        if modify and any(word in lower for word in ("rw", "archie", "含水饱和度", "sw")):
            return interaction, {"reason": "UNSUPPORTED_PARAMETER"}
        if granular or "比较上一版" in lower:
            return interaction, {"reason": "UNSUPPORTED_OPERATION"}
        if any(word in lower for word in ("什么是", "为什么这层", "为什么这段")) or (
            "为什么" in lower and any(word in lower for word in ("水层", "油层", "油水同层"))
        ):
            return interaction, {"reason": "DOMAIN_QUERY"}
        if any(
            word in lower for word in ("现在呢", "为什么失败", "为什么停", "现在怎么了", "缺什么")
        ):
            return "get_interpretation_status", task_reference
        if full or any(word in lower for word in ("再重新解释一次", "重新解释一次")):
            return "rerun_well_interpretation", task_reference
        if modify or "重新解释" in lower:
            changes: dict[str, Any] = {}
            numeric_instruction = (
                instruction.replace(well_match.group(1), "") if well_match else instruction
            )
            value = re.search(r"(?:\d*\.\d+|\d+(?:\.\d+)?%?)", numeric_instruction)
            if value:
                raw = value.group(0)
                number = float(raw.rstrip("%")) / (100 if raw.endswith("%") else 1)
                for words, parameter in (
                    (("孔隙度", "por"), "por"),
                    (("渗透率", "perm"), "perm"),
                    (("采样间隔", "sampling_interval"), "sampling_interval"),
                ):
                    if any(word in lower for word in words):
                        changes[parameter] = number
                if changes:
                    return "modify_well_interpretation", {**task_reference, **changes}
                return interaction, {
                    **task_reference,
                    "reason": "PARAMETER_NAME",
                    "known_value": number,
                }
            return interaction, {"reason": "CONFLICT"}
        if (
            any(phrase in instruction for phrase in _PREVIOUS_TASK_PHRASES)
            and "报告" in instruction
        ):
            return "get_interpretation_report", {
                **task_reference,
                "selector": "LATEST_SUCCESSFUL",
            }
        if "上一版" in instruction and "报告" in instruction:
            return "get_interpretation_report", {**task_reference, "selector": "PREVIOUS"}
        if "报告" in instruction:
            selector = "LATEST_SUCCESSFUL" if well_match else "CURRENT"
            return "get_interpretation_report", {**task_reference, "selector": selector}
        if well_match and "切回" in instruction:
            return "get_interpretation_status", task_reference
        if "全部" in instruction and ("重跑" in instruction or "重新跑" in instruction):
            return "rerun_well_interpretation", task_reference
        if any(pattern.search(instruction) for pattern in _STATUS_PATTERNS):
            return "get_interpretation_status", task_reference
        if well_match and any(word in instruction for word in ("开始", "解释")):
            return "run_well_interpretation", {"well_id": well_match.group(1)}
        return None, {}

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
        from cnlc_agent.demo.task_tools import (
            ALLOWED_TASK_TOOLS,
            TaskCommandRunner,
            TaskCommandTool,
        )

        if toolkit and any(group.mcps or group.skills_or_loaders for group in toolkit.tool_groups):
            raise ValueError("Demo Agent 不允许注册 MCP 或技能工具")
        tools = [tool for group in (toolkit.tool_groups if toolkit else []) for tool in group.tools]
        names = [tool.name for tool in tools]
        if set(names) != ALLOWED_TASK_TOOLS or len(names) != len(ALLOWED_TASK_TOOLS):
            raise ValueError("Demo Agent 必须且只能注册预定义的任务级 Tool 集合")
        tool = next(tool for tool in tools if tool.name == RUN_TOOL_NAME)
        if not isinstance(tool, RunWellInterpretationTool) or any(
            not isinstance(item, TaskCommandTool) for item in tools if item is not tool
        ):
            raise ValueError("Demo Agent 需要受控任务 Tool 实现")
        runner = next(item.runner for item in tools if isinstance(item, TaskCommandTool))
        if not isinstance(runner, TaskCommandRunner) or any(
            item.runner is not runner for item in tools if isinstance(item, TaskCommandTool)
        ):
            raise ValueError("Demo Agent 的任务 Tool 必须共享同一 TaskCommandRunner")
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
