"""Golden-snapshot support for the public-surface contract tests.

A snapshot is the recorded public surface (HTTP routes and schemas, SSE frames,
MCP tool schemas). A test fails when the surface differs from its snapshot. The
snapshot is not the contract: change the contract document first, get the
provider and affected callers to agree (AGENTS.md invariant 3), then regenerate
with ``UPDATE_GOLDEN=1 uv run --no-sync pytest -q tests/contract``.
"""

import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).parent / "golden"

HOW_TO_UPDATE = (
    "The public surface no longer matches the golden snapshot {name}.\n"
    "AGENTS.md: contract first. If the change is intended, update the contract "
    "document under docs/modules/ and get the provider's and the affected "
    "callers' owners to agree, then regenerate the snapshot with\n"
    "    UPDATE_GOLDEN=1 uv run --no-sync pytest -q tests/contract\n"
    "and commit it together with the contract change."
)


def dump(value) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


@pytest.fixture
def golden():
    def check(name: str, actual) -> None:
        path = GOLDEN / f"{name}.json"
        text = dump(actual)
        if os.environ.get("UPDATE_GOLDEN") == "1":
            path.write_text(text, encoding="utf-8")
            return
        assert path.exists(), (
            f"Missing golden snapshot {path.name}. "
            + HOW_TO_UPDATE.format(name=path.name)
        )
        expected = path.read_text(encoding="utf-8")
        assert text == expected, HOW_TO_UPDATE.format(name=path.name) + (
            f"\n\nExpected:\n{expected}\nActual:\n{text}"
        )

    return check
