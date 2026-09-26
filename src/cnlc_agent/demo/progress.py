"""把业务 Telemetry 投影为安全、稳定的首次解释进度文本。"""

from dataclasses import dataclass, field

from cnlc_agent.domain.models import JsonObject

_STAGES = (
    ("数据解编", ("W01",)),
    ("数据预处理与质量控制", ("W02", "W03")),
    ("岩性识别", ("W04",)),
    ("储层识别与物性评价", ("W05",)),
    ("流体识别", ("W06",)),
    ("油气水层分类", ("W07",)),
    ("层段划分与有效厚度", ("W08",)),
    ("综合验证", ("W09",)),
    ("最终一致性检查", ("W10",)),
)
_STAGE_BY_STEP = {step_id: stage_name for stage_name, step_ids in _STAGES for step_id in step_ids}
_STEPS_BY_STAGE = {stage_name: step_ids for stage_name, step_ids in _STAGES}
_TOOL_LABELS = {
    "company_analysis": "数据分析批量结果（Mock）",
    "company_preprocessing": "预处理批量结果（Mock）",
    "company_interpretation": "智能处理批量结果（Mock）",
    "company_report": "报告准备结果（Mock）",
    "identify_fluid": "提取批量流体结果",
    "classify_layer": "提取批量层分类结果",
    "validate_interpretation": "提取批量验证结果",
    "prepare_report": "提取报告准备结果",
    "get_well_data": "获取井资料",
    "check_curve_quality": "曲线质量检查",
    "identify_lithology": "岩性识别",
    "evaluate_petrophysics": "储层物性评价",
    "calculate_sw": "含水饱和度计算",
    "merge_intervals": "层段划分/合并",
}
_COMPLETED = {"SUCCESS", "WARNING"}
_TERMINAL = _COMPLETED | {"FAILED", "BLOCKED", "REVIEW_REQUIRED"}


@dataclass
class ExecutionProgressProjector:
    """只读取既有事件并去重展示，不参与 Workflow 状态或步骤决策。"""

    started_stages: set[str] = field(default_factory=set)
    ended_stages: set[str] = field(default_factory=set)
    started_steps: set[str] = field(default_factory=set)
    ended_steps: set[str] = field(default_factory=set)
    step_statuses: dict[str, str] = field(default_factory=dict)
    started_tools: set[tuple[str, str]] = field(default_factory=set)
    ended_tools: set[tuple[str, str]] = field(default_factory=set)
    report_started: bool = False
    report_ended: bool = False
    report_failed: bool = False
    workflow_status: str | None = None

    def project(self, name: str, attributes: JsonObject) -> list[str]:
        """将一条受控 Telemetry event 转换成零到多条用户可见文本。"""

        if name == "workflow.step.start":
            return self._step_start(attributes)
        if name == "state.change":
            return self._step_result(attributes)
        if name == "tool.start":
            return self._tool_start(attributes)
        if name == "tool.result":
            return self._tool_result(attributes)
        if name == "tool.error":
            return self._tool_error(attributes)
        if name == "workflow.result":
            status = attributes.get("status")
            if isinstance(status, str):
                self.workflow_status = status
            return []
        if (
            name == "report.start"
            and not self.report_started
            and self.workflow_status in _COMPLETED
        ):
            self.report_started = True
            return ["\n▶ 开始生成单井测井解释报告\n"]
        if name == "report.error" and self.report_started:
            self.report_failed = True
            return ["  ✗ 单井测井解释报告生成失败\n"]
        if name == "report.end" and self.report_started and not self.report_ended:
            self.report_ended = True
            if not self.report_failed:
                return ["✓ 单井测井解释报告生成完成\n"]
        return []

    def _step_start(self, attributes: JsonObject) -> list[str]:
        step_id = self._step_id(attributes)
        if step_id is None or step_id in self.started_steps:
            return []
        self.started_steps.add(step_id)
        stage = _STAGE_BY_STEP[step_id]
        messages: list[str] = []
        if stage not in self.started_stages:
            self.started_stages.add(stage)
            messages.append(f"\n▶ {stage}开始\n")
        step_name = self._safe_text(attributes.get("step_name"), step_id)
        messages.append(f"  ▶ {step_id} {step_name}\n")
        return messages

    def _step_result(self, attributes: JsonObject) -> list[str]:
        step_id = self._step_id(attributes)
        status = attributes.get("status")
        if (
            step_id is None
            or not isinstance(status, str)
            or status not in _TERMINAL
            or step_id in self.ended_steps
        ):
            return []
        self.ended_steps.add(step_id)
        self.step_statuses[step_id] = status
        step_name = self._safe_text(attributes.get("step_name"), step_id)
        if status == "SUCCESS":
            messages = [f"  ✓ {step_id} {step_name}完成\n"]
        elif status == "WARNING":
            messages = [f"  ⚠ {step_id} {step_name}完成，存在告警\n"]
        elif status == "FAILED":
            messages = [f"  ✗ {step_id} {step_name}失败\n"]
        elif status == "BLOCKED":
            messages = [f"  ■ {step_id} {step_name}停止：缺少后续处理所需资料\n"]
        else:
            messages = [f"  ◇ {step_id} {step_name}进入人工复核\n"]
        stage = _STAGE_BY_STEP[step_id]
        if stage not in self.ended_stages:
            stage_steps = _STEPS_BY_STAGE[stage]
            if all(self.step_statuses.get(item) in _COMPLETED for item in stage_steps):
                self.ended_stages.add(stage)
                stage_statuses = {self.step_statuses[item] for item in stage_steps}
                marker = "⚠" if "WARNING" in stage_statuses else "✓"
                suffix = "完成，存在告警" if marker == "⚠" else "完成"
                messages.append(f"{marker} {stage}{suffix}\n")
            elif status not in _COMPLETED:
                self.ended_stages.add(stage)
                messages.append(f"✗ {stage}未完成\n")
        return messages

    def _tool_start(self, attributes: JsonObject) -> list[str]:
        identity = self._tool_identity(attributes)
        if identity is None or identity in self.started_tools:
            return []
        self.started_tools.add(identity)
        _, tool = identity
        return [f"    → 调用工具：{tool}（{_TOOL_LABELS[tool]}）\n"]

    def _tool_result(self, attributes: JsonObject) -> list[str]:
        identity = self._tool_identity(attributes)
        status = attributes.get("status")
        if identity is None or identity in self.ended_tools or status not in _COMPLETED:
            return []
        self.ended_tools.add(identity)
        _, tool = identity
        if status == "WARNING":
            return [f"    ⚠ {tool} 执行完成，存在告警\n"]
        return [f"    ✓ {tool} 执行成功\n"]

    def _tool_error(self, attributes: JsonObject) -> list[str]:
        identity = self._tool_identity(attributes)
        if identity is None or identity in self.ended_tools:
            return []
        self.ended_tools.add(identity)
        _, tool = identity
        return [f"    ✗ {tool} 执行失败\n"]

    @staticmethod
    def _step_id(attributes: JsonObject) -> str | None:
        step_id = attributes.get("step_id")
        return step_id if isinstance(step_id, str) and step_id in _STAGE_BY_STEP else None

    @classmethod
    def _tool_identity(cls, attributes: JsonObject) -> tuple[str, str] | None:
        step_id = cls._step_id(attributes)
        tool = attributes.get("tool")
        if step_id is None or not isinstance(tool, str) or tool not in _TOOL_LABELS:
            return None
        return step_id, tool

    @staticmethod
    def _safe_text(value: object, fallback: str) -> str:
        """事件中的步骤名来自静态 Workflow 元数据，异常类型和自由文本不参与展示。"""

        return value.strip() if isinstance(value, str) and value.strip() else fallback
