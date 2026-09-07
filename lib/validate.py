"""
I2 — Schema validation utility.

Every inter-task artifact must be validated against its JSON Schema before
being written or consumed. This is the single choke point all scripts use,
so a schema change only has to be reflected in one place.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

_SCHEMA_FILENAMES = {
    "tree_snapshot": "tree_snapshot.schema.json",
    "classification": "classification.schema.json",
    "proposal": "proposal.schema.json",
    "approval_decision": "approval_decision.schema.json",
    "execution_log": "execution_log.schema.json",
    "subtree_report": "subtree_report.schema.json",
}

_validator_cache: dict[str, Any] = {}


class SchemaValidationError(Exception):
    """Raised when an artifact does not conform to its schema.

    Carries the individual jsonschema error messages so callers (and the
    Orchestrator's fail-fast gate) can report exactly what was wrong rather
    than a generic failure.
    """

    def __init__(self, schema_name: str, errors: list[str]):
        self.schema_name = schema_name
        self.errors = errors
        super().__init__(
            f"Artifact failed validation against '{schema_name}':\n"
            + "\n".join(f"  - {e}" for e in errors)
        )


def _get_validator(schema_name: str) -> Any:
    if schema_name not in _SCHEMA_FILENAMES:
        raise KeyError(
            f"Unknown schema '{schema_name}'. Known schemas: {sorted(_SCHEMA_FILENAMES)}"
        )
    if schema_name not in _validator_cache:
        schema_path = SCHEMA_DIR / _SCHEMA_FILENAMES[schema_name]
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)
        Draft202012Validator.check_schema(schema)
        _validator_cache[schema_name] = Draft202012Validator(schema)
    return _validator_cache[schema_name]


def validate(artifact: dict[str, Any], schema_name: str) -> None:
    """Validate `artifact` against the named schema.

    Raises SchemaValidationError (collecting *all* violations, not just the
    first) if invalid. Returns None on success.
    """
    validator = _get_validator(schema_name)
    errors = sorted(validator.iter_errors(artifact), key=lambda e: list(e.path))
    if errors:
        messages = [f"{list(e.path)}: {e.message}" for e in errors]
        raise SchemaValidationError(schema_name, messages)


def validate_file(path: str | Path, schema_name: str) -> dict[str, Any]:
    """Load a JSON file and validate it, returning the parsed dict."""
    with open(path, encoding="utf-8") as f:
        artifact = json.load(f)
    validate(artifact, schema_name)
    return artifact


def write_validated(artifact: dict[str, Any], schema_name: str, path: str | Path) -> None:
    """Validate then write. Never writes an artifact that fails its schema."""
    validate(artifact, schema_name)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, indent=2, sort_keys=False)
        f.write("\n")
