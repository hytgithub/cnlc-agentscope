"""AgentScope conversation shell for the existing interpretation service."""

from agentscope.agent import Agent, ContextConfig, ModelConfig, ReActConfig
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatModelBase
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from agentscope.workspace import Offloader

from cnlc_agent.demo.tools import RUN_TOOL_NAME, RunWellInterpretationTool
from cnlc_agent.demo.upload_reply import UploadInterpretationReply

DEMO_SYSTEM_PROMPT = """你是常规测井解释 Demo 的对话入口。

收到自然语言和上传井资料时，由 Demo 接入层解析文件并自动取得井标识，调用一次
run_well_interpretation。该 Tool 已封装现有 InterpretationTaskService、MainAgent 和
W01-W10；不要自行计算、猜测或复制业务流程。

Tool 返回后，用中文先给出最终状态和执行摘要，再完整展示 report_markdown。
明确说明 Demo/Mock 结果不代表真实专业解释结论。不要编造 Tool 返回中不存在的数据。
"""


class LoggingInterpretationDemoAgent(Agent):
    """AgentScope Agent that exposes only the high-level interpretation Tool."""

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
        **kwargs: object,
    ) -> None:
        del name, system_prompt, kwargs
        if model.model != "qwen-plus":
            raise ValueError("AgentScope Demo Agent 只允许使用 qwen-plus")
        tools = [
            tool
            for group in (toolkit.tool_groups if toolkit is not None else [])
            for tool in group.tools
            if tool.name == RUN_TOOL_NAME
        ]
        if len(tools) != 1:
            raise ValueError(f"Demo Agent 必须且只能注册一个 {RUN_TOOL_NAME} Tool")
        tool = tools[0]
        if not isinstance(tool, RunWellInterpretationTool):
            raise ValueError("Demo Agent 需要上传井资料适配 Tool")
        super().__init__(
            name="LoggingInterpretationDemoAgent",
            system_prompt=DEMO_SYSTEM_PROMPT,
            model=model,
            toolkit=Toolkit(tools=tools),
            middlewares=[UploadInterpretationReply(tool), *(middlewares or [])],
            state=state,
            offloader=offloader,
            model_config=model_config,
            context_config=context_config,
            react_config=react_config,
        )
