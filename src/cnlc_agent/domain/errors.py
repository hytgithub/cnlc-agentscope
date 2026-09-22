"""跨应用边界的异常统一携带稳定错误码和可重试标记。"""


class ApplicationError(Exception):
    """项目可识别异常的基类，避免上层依赖第三方异常文本。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class DataError(ApplicationError):
    """输入资料缺失、格式无效或内容不一致。"""

    pass


class ToolError(ApplicationError):
    """Tool 执行失败、超时或返回不符合 Contract。"""

    pass


class ModelError(ApplicationError):
    """模型配置、传输或响应解析错误。"""

    pass


class WorkflowError(ApplicationError):
    """Workflow 状态流转或节点输出错误。"""

    pass


class InfrastructureError(ApplicationError):
    """数据库、Redis 等基础设施错误。"""

    pass


class ValidationError(ApplicationError):
    """业务验证结果不满足约束。"""

    pass
