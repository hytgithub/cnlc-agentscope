"""真实 AgentScope ReAct/Toolkit 多轮链路，只用脚本替代公网模型响应。"""

import base64
import json
from uuid import uuid4

from agentscope.credential import DashScopeCredential
from agentscope.event import ToolResultEndEvent
from agentscope.formatter import DashScopeChatFormatter
from agentscope.message import DataBlock, TextBlock, ToolCallBlock, ToolResultBlock, UserMsg
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.tool import Toolkit
from pydantic import SecretStr

from cnlc_agent.config.settings import AppSettings, PersistenceSettings
from cnlc_agent.demo.demo_agent import DEMO_SYSTEM_PROMPT, LoggingInterpretationDemoAgent
from cnlc_agent.demo.task_tools import ALLOWED_TASK_TOOLS, TaskCommandRunner, build_task_tools


class ScriptedTaskModel(ChatModelBase):
    """离线脚本验证 Tool schema 和历史上下文传递，不评估公网模型意图准确率。"""

    def __init__(self):
        super().__init__(
            credential=DashScopeCredential(name="offline", api_key=SecretStr("test-only")),
            model="qwen-plus", parameters=self.Parameters(), stream=False,
        )
        self.formatter = DashScopeChatFormatter()
        self.seen = []

    async def _call_api(self, model_name, messages, tools=None, **kwargs):
        assert model_name == "qwen-plus"
        assert {item["function"]["name"] for item in tools} == ALLOWED_TASK_TOOLS
        assert not any(isinstance(b, DataBlock) for m in messages for b in m.content)
        self.seen.append(messages)
        last_user = max(i for i, m in enumerate(messages) if m.role == "user")
        instruction = messages[last_user].get_text_content()
        results = [b for m in messages for b in m.content if isinstance(b, ToolResultBlock)]
        current_results = [
            b for m in messages[last_user + 1:] for b in m.content if isinstance(b, ToolResultBlock)
        ]
        if current_results:
            result = current_results[-1].metadata.get("result")
            if result is None:
                output = current_results[-1].output
                result = json.loads(output if isinstance(output, str) else output[0].text)
            return ChatResponse(
                content=[TextBlock(
                    text=result.get("report_markdown") or result.get("summary", "")
                )],
                is_last=True,
            )
        if not results:
            return ChatResponse(
                content=[TextBlock(text="请先上传井资料或开始一次解释任务")], is_last=True
            )
        previous = results[-1].metadata.get("result")
        if previous is None:
            output = results[-1].output
            previous = json.loads(output if isinstance(output, str) else output[0].text)
        task_id = previous["task_id"]
        name, parameters = {
            "把孔隙度、渗透率改成0.16": (
                "modify_well_interpretation", {"por": 0.16, "perm": 0.16}
            ),
            "全部重新跑": ("rerun_well_interpretation", {}),
            "给我上一版报告": ("get_interpretation_report", {"selector": "PREVIOUS"}),
            "现在处理到哪里了？": ("get_interpretation_status", {}),
        }[instruction]
        return ChatResponse(content=[ToolCallBlock(
            id=uuid4().hex, name=name, input=json.dumps({"task_id": task_id, **parameters})
        )], is_last=True)


def upload_message(data_dir):
    return UserMsg(name="user", content=[
        TextBlock(text="帮我解释一下这口井"),
        DataBlock(name="well.json", source={
            "type": "base64", "media_type": "application/json",
            "data": base64.b64encode((data_dir / "WELL_MOCK_001.json").read_bytes()).decode(),
        }),
    ])


async def test_upload_then_react_modify_previous_full_and_status(data_dir):
    runner = TaskCommandRunner(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None),
        PersistenceSettings(persistence="memory", _env_file=None),
    )
    model = ScriptedTaskModel()
    agent = LoggingInterpretationDemoAgent(
        name="demo", system_prompt="", model=model, toolkit=Toolkit(tools=build_task_tools(runner))
    )
    events = [event async for event in agent.reply_stream(upload_message(data_dir))]
    first = next(e for e in events if isinstance(e, ToolResultEndEvent)).metadata["result"]
    assert not model.seen
    seen_results = []
    for text in ["把孔隙度、渗透率改成0.16", "给我上一版报告", "全部重新跑", "现在处理到哪里了？"]:
        events = [e async for e in agent.reply_stream(UserMsg(name="user", content=text))]
        results = [e for e in events if isinstance(e, ToolResultEndEvent)]
        assert len(results) == 1
        assert results[0].state == "success"
        seen_results.append(results[0].metadata["result"])
    modified, previous, full, status = seen_results
    assert modified["reused_steps"] == ["W01", "W02", "W03"]
    assert modified["effective_override"]["por"] == modified["effective_override"]["perm"] == 0.16
    assert previous["execution_id"] == first["execution_id"]
    assert previous["report_markdown"] == first["report_markdown"]
    assert full["execution_sequence"] == 3 and full["reused_steps"] == []
    assert full["effective_override"] == modified["effective_override"]
    assert status["execution_id"] == full["execution_id"]
    assert len(model.seen) == 8
    assert len(await runner.repository.list_executions(first["task_id"])) == 3


async def test_missing_task_enters_react_without_inventing_identity(data_dir):
    model = ScriptedTaskModel()
    agent = LoggingInterpretationDemoAgent(
        name="demo", system_prompt="", model=model, toolkit=Toolkit(tools=build_task_tools())
    )
    events = [e async for e in agent.reply_stream(UserMsg(name="user", content="把孔隙度改成0.16"))]
    assert model.seen
    assert not any(isinstance(e, ToolResultEndEvent) for e in events)
    assert "请先上传" in agent.state.context[-1].get_text_content()
    # 行为约束进入同一 ReAct 系统提示，没有第二套意图模型或步骤依赖规则。
    for rule in ("领域外", "Rw", "Archie", "含水饱和度", "16%", "PERM", "不猜", "PREVIOUS"):
        assert rule in DEMO_SYSTEM_PROMPT
