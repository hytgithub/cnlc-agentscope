"""Errors crossing application boundaries carry a stable code and retryability."""


class ApplicationError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class DataError(ApplicationError):
    pass


class ToolError(ApplicationError):
    pass


class ModelError(ApplicationError):
    pass


class WorkflowError(ApplicationError):
    pass


class InfrastructureError(ApplicationError):
    pass


class ValidationError(ApplicationError):
    pass
