"""基于 Fixture 的 Tool 仍遵循正式契约；此处不包含任何地质计算公式。"""

from cnlc_agent.application.ports import WellRepository
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import ToolError
from cnlc_agent.infrastructure.mock import FixtureRepository
from cnlc_agent.tools.contracts import ToolInput, ToolOutput


class GetWellDataTool:
    """W01 井资料加载 Tool 的 Mock 实现。"""

    name = "get_well_data"

    def __init__(self, repository: WellRepository) -> None:
        self.repository = repository

    async def execute(self, request: ToolInput) -> ToolOutput:
        """读取 Fixture，并只返回 W01 需要的井资料字段。"""

        fixture = await self.repository.load(request.well_id)
        return ToolOutput(
            status=StepStatus.SUCCESS,
            data={
                "well": fixture.well.model_dump(mode="json"),
                "raw_data": fixture.raw_data.model_dump(mode="json"),
                "requirements": fixture.requirements.model_dump(mode="json"),
            },
            metadata={"is_mock": True, "source": f"fixture:{request.well_id}"},
        )


class MockResultTool:
    """一个实例对应一个命名能力和一个 Fixture 结果键。"""

    def __init__(self, name: str, result_key: str, repository: FixtureRepository) -> None:
        self.name = name
        self.result_key = result_key
        self.repository = repository

    async def execute(self, request: ToolInput) -> ToolOutput:
        """回放指定阶段的预设结果，不执行任何专业计算。"""

        fixture = await self.repository.load(request.well_id)
        result = fixture.outputs.get(self.result_key)
        if result is None:
            raise ToolError("MOCK_TOOL_RESULT_MISSING", f"缺少演示工具结果：{self.result_key}")
        return ToolOutput(
            status=result.status,
            data=result.model_dump(mode="json"),
            warnings=result.warnings,
            metadata={"is_mock": True, "source": result.source},
        )
