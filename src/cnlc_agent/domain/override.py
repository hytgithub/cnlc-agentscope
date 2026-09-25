"""每次 Execution 的用户解释参数快照；不改写原始井资料。"""

from pydantic import Field

from cnlc_agent.domain.models import Contract


class InterpretationOverride(Contract):
    """严格校验的解释条件；PERM 正式单位与专业规则尚待确认。"""

    sampling_interval: float | None = Field(default=None, gt=0, strict=True)
    por: float | None = Field(default=None, ge=0, le=1, strict=True)
    perm: float | None = Field(default=None, ge=0, strict=True)
    prediction_model: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )

    def has_changes(self) -> bool:
        """空快照只用于默认执行，不能冒充用户修改命令。"""

        return any(value is not None for value in self.model_dump().values())
