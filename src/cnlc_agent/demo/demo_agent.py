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
from cnlc_agent.demo.tools import RUN_TOOL_NAME, RunWellInterpretationTool
from cnlc_agent.demo.upload_reply import UploadInterpretationReply

_STATUS_PATTERNS = (
    re.compile(r"(?:执行|处理|进行)?到(?:哪里|哪儿|哪一步|哪|什么地方)"),
    re.compile(r"(?:查看|当前)?进度|进度(?:怎么样|如何)"),
    re.compile(r"(?:执行|当前)状态"),
)

DEMO_SYSTEM_PROMPT = """你是单井常规测井解释任务的交互入口，只处理测井解释及相关任务操作。
支持开始解释、修改参数并重跑、全流程重跑、查询状态、查询当前或历史报告。
上传由接入层确定性解析；明确提供已知 Fixture 井号时可以调用 run_well_interpretation。
后续从已有 Tool result 中读取 task_id、execution_id、well_id，不要求用户手输这些标识。
没有可信 task_id 时，提示“请先上传井资料或开始一次解释任务”，绝不编造标识。
修改参数使用 modify_well_interpretation，仅支持 sampling_interval、por、perm、prediction_model。
用户未说参数名称（例如“改成0.16”）时先询问，不猜 POR 或 PERM。
明确的孔隙度16%可以转为 por=0.16；PERM 单位未确认，不擅自换算。
prediction_model 是专业预测配置，不得改变 qwen-plus。Rw、Archie m/n、Sw 不在参数契约内。
“重新计算含水饱和度”应说明尚不支持步骤级重跑，可以全流程重跑或修改受支持参数后重跑；
不要因此自行调用重跑或编造参数。用户明确要求全部重跑时调用 rerun_well_interpretation。
查询状态必须调用 get_interpretation_status 并依据持久事实回答，不能从聊天记忆推测。
上一版报告调用 get_interpretation_report(selector="PREVIOUS")，不要猜 execution_id；
当前版 CURRENT，最近成功版 LATEST_SUCCESSFUL。报告原文来自 Tool，完整展示，不改写专业结论。
你只决定用户意图；不得自行计算、调用内部专业 Tool、决定 W01-W10 顺序、执行起点或 RUN/REUSE。
开始、修改和全量重跑创建 Execution 后，由系统持续展示真实执行过程和对应报告。
用户询问进度时必须再次调用状态 Tool；报告未就绪时不要编造或改用旧版冒充当前版。
领域外请求不调用工具，简洁回复“当前 Agent 只处理单井常规测井解释及相关任务操作。”
Tool 错误按安全文案解释，不自动改成全量重跑。Demo/Mock 专业结果未复算，不编造结果。
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

    它只识别 Demo 已公开的任务级意图，并始终从历史 Tool Result 取得 task_id；
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
        """本地完成 AgentScope 会话标题生成，避免 Mock 凭证触发外网请求。"""

        del structured_model, kwargs
        candidates = [message.get_text_content() or "" for message in reversed(messages)]
        text = next(
            (candidate.strip() for candidate in candidates if candidate.strip()),
            "CNLC 测井解释",
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
        historical_results = [
            block
            for message in messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        task_id = self._latest_task_id(historical_results)
        if task_id is None:
            return ChatResponse(
                content=[TextBlock(text="请先上传井资料或开始一次解释任务。")],
                is_last=True,
            )
        name, parameters = self._command(instruction)
        if name is None:
            return ChatResponse(
                content=[TextBlock(text="当前 Agent 只处理单井常规测井解释及相关任务操作。")],
                is_last=True,
            )
        return ChatResponse(
            content=[
                ToolCallBlock(
                    id=uuid4().hex,
                    name=name,
                    input=json.dumps({"task_id": task_id, **parameters}, ensure_ascii=False),
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
    def _latest_task_id(cls, blocks: list[ToolResultBlock]) -> str | None:
        """从最近可信 Tool Result 读取 task_id，禁止从自然语言猜测。"""

        for block in reversed(blocks):
            task_id = cls._payload(block).get("task_id")
            if isinstance(task_id, str) and task_id:
                return task_id
        return None

    @staticmethod
    def _command(instruction: str) -> tuple[str | None, dict[str, Any]]:
        """把 Mock 联调指令映射到现有五个任务级 Tool。"""

        if "上一版" in instruction and "报告" in instruction:
            return "get_interpretation_report", {"selector": "PREVIOUS"}
        if "报告" in instruction:
            return "get_interpretation_report", {"selector": "CURRENT"}
        if "全部" in instruction and ("重跑" in instruction or "重新跑" in instruction):
            return "rerun_well_interpretation", {}
        if any(pattern.search(instruction) for pattern in _STATUS_PATTERNS):
            return "get_interpretation_status", {}
        if "重新解释" in instruction or "修改" in instruction or "改成" in instruction:
            changes: dict[str, Any] = {}
            value = re.search(r"(?:0?\.\d+|\d+(?:\.\d+)?%)", instruction)
            if value:
                raw = value.group(0)
                number = float(raw.rstrip("%")) / (100 if raw.endswith("%") else 1)
                if "孔隙度" in instruction:
                    changes["por"] = number
                if "渗透率" in instruction:
                    changes["perm"] = number
            if changes:
                return "modify_well_interpretation", changes
        return None, {}

    @classmethod
    def _render_result(cls, block: ToolResultBlock) -> str:
        """回复只复述 Tool 的持久事实，不推断专业结果。"""

        payload = cls._payload(block)
        if report := payload.get("report_markdown"):
            return str(report)
        sequence = payload.get("execution_sequence")
        status = payload.get("execution_status")
        current = payload.get("current_step") or "暂无运行步骤"
        completed = "、".join(payload.get("completed_steps") or []) or "无"
        if payload.get("command") == "STATUS":
            return (
                f"Execution #{sequence} 状态为 {status}；当前步骤：{current}；已完成：{completed}。"
            )
        return f"解释任务已提交：Execution #{sequence}，状态 {status}。"


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
