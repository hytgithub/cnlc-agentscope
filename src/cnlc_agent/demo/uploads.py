"""按现有 Demo Schema 解码官方 Web UI 的附件消息块。"""

import base64
import binascii
import hashlib
import json

from agentscope.message import Base64Source, DataBlock, Msg, TextBlock
from pydantic import ValidationError

from cnlc_agent.demo.upload_adapters import adapt_synthetic_context
from cnlc_agent.domain.models import MockFixture

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


class UploadError(ValueError):
    """可安全展示给用户的附件错误，不携带解析器堆栈或原始内容。"""


def parse_upload(messages: list[Msg]) -> tuple[MockFixture, str]:
    """从当前轮消息中提取一份 JSON Fixture 和自然语言指令。"""

    attachments: list[bytes] = []
    instructions: list[str] = []
    for message in messages:
        if message.role != "user":
            continue
        for block in message.content:
            if isinstance(block, DataBlock):
                if not isinstance(block.source, Base64Source):
                    raise UploadError("请使用浏览器上传 JSON 文件，不支持文件路径或远程链接。")
                if len(block.source.data) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4:
                    raise UploadError("井资料文件不能超过 5 MiB。")
                try:
                    attachments.append(base64.b64decode(block.source.data, validate=True))
                except (ValueError, binascii.Error):
                    raise UploadError("附件编码无效，请重新上传 JSON 文件。") from None
            elif isinstance(block, TextBlock):
                if block.text.startswith("[File: ") and "]\n" in block.text:
                    attachments.append(block.text.split("]\n", 1)[1].encode("utf-8"))
                else:
                    instructions.append(block.text)
    if len(attachments) != 1:
        raise UploadError("请上传一份井资料 JSON，并输入“帮我解释一下这口井”。每次只解释一口井。")
    content = attachments[0]
    if len(content) > MAX_UPLOAD_BYTES:
        raise UploadError("井资料文件不能超过 5 MiB。")
    try:
        data = json.loads(content)
        if not isinstance(data, dict) or not isinstance(data.get("well"), dict):
            raise ValueError("missing well")
        # 缺少业务井号时从规范化 JSON 生成稳定标识，避免随机 ID 进入内容摘要。
        if not data["well"].get("well_id"):
            canonical = json.dumps(
                data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            data["well"]["well_id"] = (
                f"UPLOAD_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:24].upper()}"
            )
        try:
            fixture = MockFixture.model_validate(data)
        except ValidationError:
            # 标准 MockFixture 校验失败后，只尝试项目明确支持的合成上下文适配器。
            adapted = adapt_synthetic_context(data)
            if adapted is None:
                raise
            fixture = adapted
    except (ValueError, UnicodeError, ValidationError, RecursionError):
        raise UploadError(
            "井资料格式无效：当前 Demo 支持 mock_data 示例格式，或明确标记为 "
            "synthetic_single_well_interpretation_context 的合成单井解释上下文 JSON。"
            "暂不支持原始 LAS/GDSX/CSV。"
        ) from None
    if not fixture.raw_data.depths or not fixture.raw_data.curves:
        raise UploadError("井资料中没有深度采样或测井曲线，请检查上传文件。")
    return fixture, "\n".join(instructions).strip() or "执行上传井资料的测井解释演示"
