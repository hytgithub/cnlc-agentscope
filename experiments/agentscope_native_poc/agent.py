"""构建条件完全一致、仅 Skill 注册状态不同的 AgentScope 实验 Agent。"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from agentscope.agent import Agent, ReActConfig
from agentscope.model import ChatModelBase
from agentscope.skill import LocalSkillLoader
from agentscope.tool import Toolkit

from .state import MockState
from .tools import build_mock_tools

POC_ROOT = Path(__file__).resolve().parent
SKILLS_ROOT = POC_ROOT / "skills"

SYSTEM_PROMPT = (
    "你是单井常规测井解释助手。正式业务事实必须来自 Toolkit 中的 Tool，"
    "不得编造专业测井结果；只执行已提供的能力。修改请求必须先做预检，"
    "对象或范围不明确时禁止写入。查询优先读取已有结果，"
    "不启动不必要的完整解释。能力不支持时如实说明。"
)


class ExperimentConfig(StrEnum):
    """A/B 实验配置；除此枚举外模型、Prompt、Tool 和输入均保持相同。"""

    NO_SKILL = "no_skill"
    WITH_SKILL = "with_skill"


def build_toolkit(
    config: ExperimentConfig | str,
    state: MockState | None = None,
) -> Toolkit:
    """构建相同 Mock Tool 集；with_skill 额外加载三个本地 AgentScope Skill。"""

    selected = ExperimentConfig(config)
    mock_state = state or MockState.fixture()
    skills = (
        [LocalSkillLoader(str(SKILLS_ROOT), scan_subdir=True)]
        if selected == ExperimentConfig.WITH_SKILL
        else []
    )
    return Toolkit(tools=build_mock_tools(mock_state), skills_or_loaders=skills)


def build_agent(
    model: ChatModelBase,
    config: ExperimentConfig | str,
    state: MockState | None = None,
) -> Agent:
    """构建单个 AgentScope ReAct Agent；业务行为只经本实验 Toolkit。"""

    return Agent(
        name="Task012ANativeCapabilityPoc",
        system_prompt=SYSTEM_PROMPT,
        model=model,
        toolkit=build_toolkit(config, state),
        react_config=ReActConfig(max_iters=8),
    )
