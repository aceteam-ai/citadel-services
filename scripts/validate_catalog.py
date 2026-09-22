#!/usr/bin/env python3
"""Validate the trusted registry and every service manifest."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def validate(document: Any, schema: Any, label: str) -> list[str]:
    validator = Draft202012Validator(schema)
    errors: list[str] = []
    for error in sorted(
        validator.iter_errors(document), key=lambda item: list(item.path)
    ):
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        errors.append(f"{label}:{location}: {error.message}")
    return errors


def main() -> int:
    registry_schema = load_yaml(ROOT / "schema" / "registry-schema.yaml")
    service_schema = load_yaml(ROOT / "schema" / "service-schema.yaml")
    Draft202012Validator.check_schema(registry_schema)
    Draft202012Validator.check_schema(service_schema)

    failures = validate(
        load_yaml(ROOT / "registry.yaml"), registry_schema, "registry.yaml"
    )
    for manifest in sorted((ROOT / "services").glob("*/service.yaml")):
        failures.extend(
            validate(
                load_yaml(manifest),
                service_schema,
                manifest.relative_to(ROOT).as_posix(),
            )
        )

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1

    print("catalog schemas valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
