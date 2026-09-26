"""将现有测井工具的确定性执行逻辑接入 AgentScope Tool 协议。"""

import asyncio
import json
import logging
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

logger = logging.getLogger(__name__)


class AgentScopeJsonTool(ToolBase):
    """把旧工具的 JSON 参数与结果转换成 AgentScope 的关键字参数和 ToolChunk。"""

    name = ""
    description = ""
    parameters: list[dict[str, Any]] | dict[str, Any] = []
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    # 部分子工具会写 GDSX 文件，同名路径不可并行修改。
    is_concurrency_safe = False
    is_read_only = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # 旧参数清单只用于生成 AgentScope JSON Schema；执行时不依赖旧框架。
        if isinstance(cls.parameters, dict):
            cls.input_schema = cls.parameters
            return
        properties = {}
        required = []
        for parameter in cls.parameters:
            kind = parameter["type"]
            schema: dict[str, Any] = {
                "type": "boolean" if kind == "bool" else kind,
                "description": parameter.get("description", ""),
            }
            if kind == "array":
                schema["items"] = {"type": "string"}
            properties[parameter["name"]] = schema
            if parameter.get("required"):
                required.append(parameter["name"])
        cls.input_schema = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    @staticmethod
    def _verify_json_format_args(value: str | dict[str, Any]) -> dict[str, Any]:
        """供原有解析方法复用，并明确拒绝非对象输入。"""
        result = json.loads(value) if isinstance(value, str) else value
        if not isinstance(result, dict):
            raise ValueError("工具参数必须是 JSON 对象")
        return result

    async def check_permissions(self, *_args: Any, **_kwargs: Any) -> PermissionDecision:
        """工具由明确的 AgentScope 调用触发；路径及外部服务由业务层配置。"""
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="用户请求执行测井工具时允许调用。",
        )

    async def call(self, **kwargs: Any) -> ToolChunk:
        """在线程中执行同步文件或 HTTP 操作，返回 AgentScope 标准结果。"""
        try:
            raw = await asyncio.to_thread(self.run, kwargs)
            data = json.loads(raw)
            failed = (
                data.get("success") is False
                or ("code" in data and data["code"] != 200)
            ) if isinstance(data, dict) else False
            return ToolChunk(
                content=[TextBlock(text=raw)],
                state=ToolResultState.ERROR if failed else ToolResultState.SUCCESS,
                metadata={"result": data},
            )
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            logger.warning("测井工具 %s 执行失败: %s", self.name, type(exc).__name__)
            return ToolChunk(
                content=[
                    TextBlock(
                        text=json.dumps(
                            {"success": False, "message": "工具执行失败"},
                            ensure_ascii=False,
                        )
                    )
                ],
                state=ToolResultState.ERROR,
                metadata={"error_code": "WPLM_TOOL_FAILED"},
            )

    def run(self, params: dict[str, Any]) -> str:
        """执行原有业务逻辑并返回 JSON 字符串，由子类实现。"""
        raise NotImplementedError
