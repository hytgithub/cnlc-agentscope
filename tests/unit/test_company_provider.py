"""ProviderCall 模型自身保护生命周期、大小和敏感数据边界。"""

import pytest
from pydantic import ValidationError

from cnlc_agent.domain.company_provider import (
    CompanyProviderCall,
    CompanyProviderCallStatus,
    CompanyProviderOperation,
)
from cnlc_agent.domain.models import utc_now
from cnlc_agent.infrastructure.company_api import CompanyApiSettings
from cnlc_agent.infrastructure.company_provider import CompanyProviderSettings


def _running(**updates):
    payload = {
        "task_id": "task",
        "execution_id": "execution",
        "input_version_id": "input",
        "provider_operation": CompanyProviderOperation.PREPROCESSING,
        "request_summary": {"well_id": "well"},
    }
    payload.update(updates)
    return CompanyProviderCall(**payload)


def test_provider_call_lifecycle_and_safe_success():
    running = _running()
    success = CompanyProviderCall.model_validate(
        {
            **running.model_dump(mode="python"),
            "status": CompanyProviderCallStatus.SUCCESS,
            "normalized_result": {"logReqJson": {"curves": ["GR"]}},
            "finished_at": utc_now(),
        }
    )
    assert success.normalized_result["logReqJson"] == {"curves": ["GR"]}
    assert success.error_code is None


def test_provider_call_unknown_requires_safe_terminal_error():
    """结果未知是可查询终态，不得携带未经确认的业务结果。"""

    running = _running()
    unknown = CompanyProviderCall.model_validate(
        {
            **running.model_dump(mode="python"),
            "status": CompanyProviderCallStatus.UNKNOWN,
            "finished_at": utc_now(),
            "error_code": "COMPANY_TIMEOUT",
        }
    )
    assert unknown.provider_call_id
    assert unknown.normalized_result == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"token": "secret"},
        {"Authorization": "secret"},
        {"nested": {"server_path": "opaque"}},
        {"path": "/private/input.gdsx"},
        {"gdsx_content": "bytes"},
    ],
)
def test_provider_call_rejects_sensitive_fields_and_paths(payload):
    with pytest.raises(ValidationError):
        _running(request_summary=payload)


def test_provider_call_rejects_oversized_result_without_truncation():
    running = _running()
    with pytest.raises(ValidationError, match="PROVIDER_RESULT_TOO_LARGE"):
        CompanyProviderCall.model_validate(
            {
                **running.model_dump(mode="python"),
                "status": CompanyProviderCallStatus.SUCCESS,
                "normalized_result": {"resultData": "x" * (513 * 1024)},
                "finished_at": utc_now(),
            }
        )


def test_real_provider_settings_are_explicit_and_required():
    with pytest.raises(ValidationError):
        CompanyApiSettings(_env_file=None)
    with pytest.raises(ValidationError):
        CompanyProviderSettings(_env_file=None)
