"""公司批量结果到细分步骤的投影；投影不代表重新调用专业算法。"""

from copy import deepcopy

from pydantic import Field

from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import ToolError
from cnlc_agent.domain.models import Contract, JsonObject, StageResult
from cnlc_agent.domain.state import StatePatch
from cnlc_agent.infrastructure.company_api import CompanyCallResult


class CompanyStepProjection(Contract):
    """一次执行内多个步骤共享外部调用来源，缺少内容不会继承整批成功状态。"""

    execution_id: str
    input_version_id: str
    external_call_id: str
    operation: str
    steps: dict[StepId, StageResult] = Field(default_factory=dict)

    def state_patch(self, execution_id: str, input_version_id: str) -> StatePatch:
        """仅产生已获得的专业结果补丁，不修改步骤完成状态或伪造原始曲线。"""
        if execution_id != self.execution_id or input_version_id != self.input_version_id:
            raise ToolError("COMPANY_RESULT_VERSION_MISMATCH", "结果不属于当前执行及数据版本")
        fields = {
            StepId.W03: "qc_result",
            StepId.W04: "lithology_result",
            StepId.W05: "petrophysics_result",
            StepId.W06: "fluid_result",
            StepId.W07: "layer_classification",
            StepId.W08: "interval_result",
        }
        return StatePatch.model_validate(
            {
                fields[step]: result.model_copy(deep=True)
                for step, result in self.steps.items()
                if step in fields
            }
        )


def project_prediction(call: CompanyCallResult) -> CompanyStepProjection:
    """按旧后处理代码确认的原始字段归类，不臆造分类、单位、层段合并和复核结论。"""
    if call.operation != "interpretation" or not isinstance(call.data, dict):
        raise ToolError("COMPANY_RESULT_KIND_MISMATCH", "需要公司专业预测结果")
    items = call.data.get("resultData")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ToolError("COMPANY_PREDICTION_DATA_INVALID", "预测结果必须对应单口井")
    well = items[0]
    curve_list = well.get("curveList", [])
    if not isinstance(curve_list, list):
        raise ToolError("COMPANY_CURVE_DATA_INVALID", "预测曲线列表格式错误")
    curves: dict[str, JsonObject] = {}
    for curve in curve_list:
        if not isinstance(curve, dict):
            raise ToolError("COMPANY_CURVE_DATA_INVALID", "预测曲线条目必须为对象")
        name = curve.get("standardName") or curve.get("name")
        if not isinstance(name, str) or not name:
            raise ToolError("COMPANY_CURVE_NAME_MISSING", "预测曲线缺少名称")
        if name in curves:
            raise ToolError("COMPANY_CURVE_AMBIGUOUS", "预测结果存在同名曲线，需明确版本")
        values = curve.get("curveData")
        if values is not None and not isinstance(values, list):
            raise ToolError("COMPANY_CURVE_DATA_INVALID", "预测曲线采样必须为数组")
        if values:
            curves[name] = deepcopy(curve)

    # 分组只使用已有代码中的参数名；组分曲线不直接等同于岩性分类结论。
    groups = {
        StepId.W04: ("SAND", "LIME", "DOLO", "CARB", "ANHY"),
        StepId.W05: ("POR", "PERM", "SH"),
        StepId.W06: ("SW",),
        StepId.W07: ("JSJL",),
        StepId.W08: (),
    }
    projection = CompanyStepProjection(
        execution_id=call.execution_id,
        input_version_id=call.input_version_id,
        external_call_id=call.external_call_id,
        operation=call.operation,
    )
    for step, names in groups.items():
        data: JsonObject = {
            "external_call_id": call.external_call_id,
            "input_version_id": call.input_version_id,
            "prediction_curves": {name: curves[name] for name in names if name in curves},
        }
        received = bool(data["prediction_curves"])
        if step == StepId.W07 and isinstance(well.get("JSJL"), list) and well["JSJL"]:
            data["classification_samples"] = deepcopy(well["JSJL"])
            received = True
        if (
            step == StepId.W08
            and isinstance(well.get("ogResultList"), list)
            and well["ogResultList"]
        ):
            data["source_intervals"] = deepcopy(well["ogResultList"])
            received = True
        projection.steps[step] = StageResult(
            status=StepStatus.REVIEW_REQUIRED if received else StepStatus.BLOCKED,
            result=data,
            is_mock=False,
            source=f"company:interpretation:{call.external_call_id}",
            evidence=["已接收公司预测响应中的原始字段"] if received else [],
            missing_evidence=[] if received else ["公司响应未提供本功能所需数据"],
            warnings=["原始字段已归类；单位、深度和专业结论映射尚待真实样本核对"]
            if received
            else [],
            recommended_action="review_company_result" if received else "request_missing_result",
        )
    return projection
