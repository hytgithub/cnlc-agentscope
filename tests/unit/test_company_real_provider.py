"""真实公司批次提供者的离线契约测试，不访问公司网络。"""

from pathlib import Path

import pytest
from pydantic import SecretStr

from cnlc_agent.domain.enums import StepId, StepStatus, ValidationStatus
from cnlc_agent.domain.models import DataRequirements, LogCurve, RawData, Well, WellData, utc_now
from cnlc_agent.infrastructure.company_api import (
    CompanyApiSettings,
    CompanyCallResult,
    PreprocessingResult,
)
from cnlc_agent.tools.company_real import RealCompanyBatchProvider, _operations_with_depth_range
from cnlc_agent.tools.contracts import ToolInput


class FakeCompanyClient:
    """只记录正式调用参数，避免测试需要内网服务或真实凭据。"""

    def __init__(self) -> None:
        self.preprocess_calls = []
        self.predict_calls = []
        self.model_calls = []

    async def list_models(self, parameters):
        self.model_calls.append(parameters)
        return [{"id": "service-1", "modelName": "test"}]

    async def preprocess(self, path, operations, execution_id, input_version_id):
        self.preprocess_calls.append((path, operations, execution_id, input_version_id))
        return PreprocessingResult(
            call=CompanyCallResult(
                operation="preprocessing",
                execution_id=execution_id,
                input_version_id=input_version_id,
                data={"logReqJson": {"GR": [10.0]}, "operations": operations},
                started_at=utc_now(),
                finished_at=utc_now(),
            ),
            gdsx_content=b"processed",
        )

    async def predict(self, parameters, execution_id, input_version_id):
        self.predict_calls.append((parameters, execution_id, input_version_id))
        return CompanyCallResult(
            operation="interpretation",
            execution_id=execution_id,
            input_version_id=input_version_id,
            data={
                "resultData": [
                    {
                        "curveList": [
                            {"standardName": name, "curveData": [0.1]}
                            for name in (
                                "SAND",
                                "LIME",
                                "DOLO",
                                "CARB",
                                "ANHY",
                                "POR",
                                "PERM",
                                "SH",
                                "SW",
                            )
                        ],
                        "JSJL": ["油层"],
                        "ogResultList": [{"sdep": 1.0, "edep": 2.0, "result": "油层"}],
                    }
                ]
            },
            started_at=utc_now(),
            finished_at=utc_now(),
        )

    async def aclose(self):
        return None


def fake_well_data(_path: Path, well_id: str) -> WellData:
    return WellData(
        well=Well(
            well_id=well_id,
            name="processed-well" if _path.name == "processed.gdsx" else "source-well",
        ),
        raw_data=RawData(depths=[1.0], curves={"GR": LogCurve(unit="unknown", values=[10.0])}),
        requirements=DataRequirements(required_curves=["GR"]),
    )


def fake_standardize(path: Path, directory: Path):
    target = directory / "standardized.gdsx"
    target.write_bytes(path.read_bytes())
    return target, [{"standard_name": "GR", "raw_name": "GR_EDTC"}]


def fake_postprocess(processed_path: Path, final_path: Path, _prediction):
    final_path.write_bytes(processed_path.read_bytes())
    return {"success": True, "status": "success"}, [{"series": []}]


def fake_preprocess_operations(_path: Path, interval: float):
    return {
        "curves": [
            "AC", "CAL", "CNL", "DEN", "GR", "PE", "RT", "RXO", "SP",
            "UPOSX", "UPOSY", "UPOSZ",
        ],
        "startDepth": 1.0,
        "endDepth": 2.0,
        "curveNameStandard": {"enable": True, "index": 1, "data": {}},
        "curveUnitStandard": {"enable": True, "index": 2, "data": {"CAL": "cm", "GR": "API"}},
        "resample": {"enable": True, "index": 3, "data": {"defaultInterval": interval}},
        "wellCoordinateGenerate": {"enable": True, "index": 4, "data": True},
    }


@pytest.mark.asyncio
async def test_real_provider_uses_one_preprocess_and_prediction_for_execution(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.gdsx"
    source.write_bytes(b"source")
    settings = CompanyApiSettings(
        preprocessing_url="http://preprocess.test/",
        prediction_url="http://predict.test/",
        token=SecretStr("test-token"),
        gdsx_path=source,
        service_id="service-1",
        create_people="tester",
        resample_interval=0.1,
        _env_file=None,
    )
    monkeypatch.setattr("cnlc_agent.tools.company_real._well_data", fake_well_data)
    monkeypatch.setattr("cnlc_agent.tools.company_real._standardize_source", fake_standardize)
    monkeypatch.setattr(
        "cnlc_agent.tools.company_real._preprocess_operations", fake_preprocess_operations
    )
    monkeypatch.setattr("cnlc_agent.tools.company_real._postprocess_prediction", fake_postprocess)
    client = FakeCompanyClient()
    provider = RealCompanyBatchProvider(client, settings, tmp_path / "outputs")  # type: ignore[arg-type]
    request = ToolInput(
        task_id="task-1",
        trace_id="trace-1",
        well_id="WELL_001",
        step_id=StepId.W01,
        parameters={"execution_id": "execution-1", "input_version_id": "input-1"},
    )

    analysis = await provider.execute("analysis", request)
    preprocessing = await provider.execute("preprocessing", request)
    interpretation = await provider.execute("interpretation", request)

    assert analysis["well_data"]["well"]["name"] == "source-well"
    assert preprocessing["qc"]["is_mock"] is False
    assert preprocessing["operations_applied"] is True
    assert len(client.preprocess_calls) == 1
    assert len(client.model_calls) == 1
    assert len(client.predict_calls) == 1
    operations = client.preprocess_calls[0][1]
    assert operations["curves"] == [
        "AC", "CAL", "CNL", "DEN", "GR", "PE", "RT", "RXO", "SP",
        "UPOSX", "UPOSY", "UPOSZ",
    ]
    assert operations["resample"]["data"]["defaultInterval"] == 0.1
    assert client.predict_calls[0][0]["wellName"] == "processed-well"
    assert client.predict_calls[0][0]["logReqJson"] == {"GR": [10.0]}
    assert client.predict_calls[0][0]["encodingUrl"] == ""
    assert client.predict_calls[0][0]["taskConfig"]["NUM"] == [
        "POR", "SW", "PERM", "SH", "SAND", "LIME", "DOLO", "CARB", "ANHY"
    ]
    assert interpretation["petrophysics"]["is_mock"] is False
    assert interpretation["petrophysics"]["status"] == StepStatus.WARNING
    assert interpretation["validation"]["status"] == StepStatus.WARNING
    assert interpretation["validation"]["validation_status"] == ValidationStatus.CONSISTENT
    assert interpretation["validation"]["is_mock"] is False
    assert interpretation["validation"]["result"]["prediction_result_complete"] is True


def test_depth_range_matches_yuce_well_info_rule(monkeypatch, tmp_path):
    """缺少配置时，以可识别曲线的公共区间补齐，而不覆写显式配置。"""

    class Curve:
        def __init__(self, start, end):
            self.dimension1Start = start
            self.dimension1End = end

    monkeypatch.setattr(
        "cnlc_agent.tools.company_real.get_gdx_curve_name_list", lambda _path: ["GR", "AC", "OTHER"]
    )
    curves = {"GR": Curve(100.123, 200.876), "AC": Curve(110.456, 190.654)}
    monkeypatch.setattr(
        "cnlc_agent.tools.company_real.get_gdx_curve_info", lambda _path, name: curves[name]
    )

    result = _operations_with_depth_range(tmp_path / "source.gdsx", {"curves": ["GR", "AC"]})

    assert result["startDepth"] == 110.46
    assert result["endDepth"] == 190.65
