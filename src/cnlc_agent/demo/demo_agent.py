"""现有测井解释应用服务的 AgentScope 对话外壳。"""

from agentscope.agent import Agent, ContextConfig, ModelConfig, ReActConfig
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatModelBase
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from agentscope.workspace import Offloader

from cnlc_agent.demo.tools import RUN_TOOL_NAME, RunWellInterpretationTool
from cnlc_agent.demo.upload_reply import UploadInterpretationReply

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
当前工具同步执行，不宣称有后台 Worker、暂停或任意时刻状态查询能力。
领域外请求不调用工具，简洁回复“当前 Agent 只处理单井常规测井解释及相关任务操作。”
Tool 错误按安全文案解释，不自动改成全量重跑。Demo/Mock 专业结果未复算，不编造结果。
"""


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
        **kwargs: object,
    ) -> None:
        # 忽略前端自定义名称和提示词，保证 Demo 始终使用项目约束的系统提示。
        del name, system_prompt, kwargs
        if model.model != "qwen-plus":
            raise ValueError("AgentScope Demo Agent 只允许使用 qwen-plus")
        from cnlc_agent.demo.task_tools import ALLOWED_TASK_TOOLS, TaskCommandTool

        if toolkit and any(
            group.mcps or group.skills_or_loaders for group in toolkit.tool_groups
        ):
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
