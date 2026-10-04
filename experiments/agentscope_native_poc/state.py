"""Task 012A 专用内存状态与调用轨迹；不连接正式业务持久化。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class ToolCallRecord:
    """一条 Mock Tool 调用记录，供离线断言和 A/B 证据输出。"""

    sequence: int
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


@dataclass
class MockState:
    """可重建的 Task 012A 测试 Fixture，不代表真实井、任务或解释结果。"""

    task_id: str = "TASK-012A-FIXTURE"
    well_id: str = "WELL-A"
    current_execution_id: str = "V2"
    previous_execution_id: str = "V1"
    view_scope: str = "2035-2038m"
    pending_stage: str | None = "intelligent_processing"
    next_execution_number: int = 3
    known_execution_ids: set[str] = field(default_factory=lambda: {"V1", "V2"})
    trace: list[ToolCallRecord] = field(default_factory=list)
    approved_change: dict[str, Any] | None = None

    @classmethod
    def fixture(cls) -> MockState:
        """每条场景都从相同的、明确标为合成数据的基线开始。"""

        return cls()

    def context(self) -> dict[str, Any]:
        """返回实验事实来源和当前 Mock 上下文。"""

        return {
            "source": "task_012a_test_fixture",
            "is_real_business_data": False,
            "task_id": self.task_id,
            "well_id": self.well_id,
            "current_execution_id": self.current_execution_id,
            "previous_execution_id": self.previous_execution_id,
            "known_execution_ids": sorted(self.known_execution_ids),
            "view_scope": self.view_scope,
            "pending_stage": self.pending_stage,
        }

    def new_execution(self) -> str:
        """推进 Fixture 版本号；只模拟版本变化，不执行测井计算。"""

        self.previous_execution_id = self.current_execution_id
        execution_id = f"V{self.next_execution_number}"
        self.next_execution_number += 1
        self.current_execution_id = execution_id
        self.known_execution_ids.add(execution_id)
        return execution_id

    def record(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> ToolCallRecord:
        """按调用顺序保存契约级输入与结果，不保留模型隐藏推理。"""

        record = ToolCallRecord(
            sequence=len(self.trace) + 1,
            call_id=uuid4().hex,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
        )
        self.trace.append(record)
        return record
