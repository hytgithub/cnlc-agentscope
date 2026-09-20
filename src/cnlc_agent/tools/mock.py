"""Fixture-based tools share the formal contract; there are no geological formulas here."""

from cnlc_agent.application.ports import WellRepository
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import ToolError
from cnlc_agent.infrastructure.mock import FixtureRepository
from cnlc_agent.tools.contracts import ToolInput, ToolOutput


class GetWellDataTool:
    name = "get_well_data"

    def __init__(self, repository: WellRepository) -> None:
        self.repository = repository

    async def execute(self, request: ToolInput) -> ToolOutput:
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
    """One instance = one named capability and one fixture result key."""

    def __init__(self, name: str, result_key: str, repository: FixtureRepository) -> None:
        self.name = name
        self.result_key = result_key
        self.repository = repository

    async def execute(self, request: ToolInput) -> ToolOutput:
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
