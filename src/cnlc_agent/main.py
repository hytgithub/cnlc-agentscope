"""Offline skeleton runner. Unsupported runtime modes must fail explicitly."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from pydantic import ValidationError as SchemaError

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import ApplicationError
from cnlc_agent.domain.models import TaskRequest


def main() -> int:
    parser = argparse.ArgumentParser(description="测井解释 Task 01 Mock 骨架演示")
    parser.add_argument("--well-id", default="WELL_MOCK_001")
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
        state, markdown = asyncio.run(build_application(settings).run(request))
        output = settings.output_dir / request.task_id
        output.mkdir(parents=True, exist_ok=False)
        (output / "result.json").write_text(state.model_dump_json(indent=2), encoding="utf-8")
        (output / "report.md").write_text(markdown, encoding="utf-8")
    except (ApplicationError, OSError) as exc:
        print(f"运行或输出失败：{type(exc).__name__}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "task_id": request.task_id,
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
