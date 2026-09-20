"""Export current contracts and validate interchange files without starting a workflow."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from cnlc_agent.domain.models import (
    Contract,
    MockFixture,
    StageResult,
    TaskRequest,
    ValidationResult,
    WellData,
)
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.tools.contracts import ToolInput, ToolOutput

CONTRACTS: dict[str, type[Contract]] = {
    "task-request": TaskRequest,
    "well-data": WellData,
    "mock-fixture": MockFixture,
    "stage-result": StageResult,
    "validation-result": ValidationResult,
    "interpretation-state": InterpretationState,
    "tool-input": ToolInput,
    "tool-output": ToolOutput,
}


def schema_text(model: type[Contract]) -> str:
    schema = model.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def export_schemas(directory: Path, *, check: bool = False) -> bool:
    if not check:
        directory.mkdir(parents=True, exist_ok=True)
    matches = True
    for name, model in CONTRACTS.items():
        path = directory / f"{name}.schema.json"
        expected = schema_text(model)
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                print(f"Schema mismatch: {path}", file=sys.stderr)
                matches = False
        else:
            path.write_text(expected, encoding="utf-8")
    return matches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--output-dir", type=Path, default=Path("schemas"))
    export.add_argument("--check", action="store_true")
    validate = commands.add_parser("validate")
    validate.add_argument("contract", choices=sorted(CONTRACTS))
    validate.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            return 0 if export_schemas(args.output_dir, check=args.check) else 1
        CONTRACTS[args.contract].model_validate_json(args.path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        # Only field paths and error codes: never echo well data or arbitrary input values.
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            location = ".".join(str(part) for part in error["loc"]) or "<root>"
            print(f"{location}: {error['type']}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("Unable to read or write contract files.", file=sys.stderr)
        return 2
    print(f"Valid: {args.contract}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
