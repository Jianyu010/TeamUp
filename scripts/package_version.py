"""Hatch version source for the Client and Server distributions."""

import os
import re
from pathlib import Path

from scripts.ci.version import checkout_build

_BUILD_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}(?:\.dev[1-9][0-9]*)?"
)


def package_version(root: Path) -> str:
    build = checkout_build(root)
    if build is not None:
        return build.version

    stamp = root / "TEAMUP-VERSION"
    version = (
        stamp.read_text(encoding="utf-8").strip()
        if stamp.is_file()
        else os.environ.get("TEAMUP_VERSION")
    )
    if version is None:
        return "0.0.0+unknown"
    if _BUILD_VERSION.fullmatch(version) is None:
        raise ValueError(
            "Expected X.Y.Z or X.Y.Z.devN from TEAMUP_VERSION or TEAMUP-VERSION"
        )
    return version


__version__ = package_version(Path(__file__).resolve().parents[1])
