"""Mock business runner with explicit memory or PostgreSQL/Redis persistence."""

import argparse
import asyncio
import json
import logging
import re
import sys
from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError as SchemaError

from cnlc_agent.application.runtime import application_runtime
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import ApplicationError
from cnlc_agent.domain.models import TaskRequest
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.reports.assembler import ReportAssembler


def main() -> int:
    parser = argparse.ArgumentParser(description="测井解释 Mock 业务运行与历史查询")
    # parser.add_argument("--well-id", default="WELL_DI73_56H_LAYER64")
    parser.add_argument("--well-id", default="WELL_MOCK_001")
    parser.add_argument("--task-id", help="查询历史任务及报告，不重新执行")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    try:
        overrides = {}
        if args.data_dir is not None:
            overrides["mock_data_dir"] = args.data_dir
        if args.output_dir is not None:
            overrides["output_dir"] = args.output_dir
        settings = AppSettings(**overrides)
        request = TaskRequest(well_id=args.well_id)
    except SchemaError as exc:
        # Field locations are useful; raw configuration values may be secrets.
        fields = [".".join(str(part) for part in item["loc"]) for item in exc.errors()]
        print(f"配置或输入无效：{', '.join(fields)}", file=sys.stderr)
        return 2
    logging.basicConfig(level=settings.log_level, format="%(message)s")
    try:

        async def execute() -> tuple[InterpretationState, str]:
            async with application_runtime(settings) as app:
                if args.task_id:
                    stored = await app.repository.get(args.task_id)
                    if stored is None:
                        raise ApplicationError("TASK_NOT_FOUND", "找不到历史任务")
                    report = await app.repository.get_report(args.task_id)
                    if not report:
                        raise ApplicationError("REPORT_NOT_READY", "任务报告尚未完成")
                    return stored, report
                return await app.run(request)

        state, markdown = asyncio.run(execute())
        output_name = (
            state.task.task_id
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", state.task.task_id)
            else sha256(state.task.task_id.encode()).hexdigest()
        )
        output = settings.output_dir / output_name
        output.mkdir(parents=True, exist_ok=False)
        (output / "result.json").write_text(ReportAssembler().to_json(state), encoding="utf-8")
        (output / "report.md").write_text(markdown, encoding="utf-8")
    except (ApplicationError, OSError, SchemaError) as exc:
        code = exc.code if isinstance(exc, ApplicationError) else type(exc).__name__
        print(f"运行或输出失败：{code}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "task_id": state.task.task_id,
                "mode": settings.mode,
                "status": state.status.value,
                "result": str(output / "result.json"),
                "report": str(output / "report.md"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if state.status in {StepStatus.SUCCESS, StepStatus.WARNING} else 1


if __name__ == "__main__":
    raise SystemExit(main())
