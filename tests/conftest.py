"""Collect the suites that have their own CI job only when they are named.

``tests/install`` and ``tests/system`` start processes and build wheels, so the
default ``pytest`` run skips them. Their jobs in .github/workflows/pr.yml pass
the directory explicitly, which turns collection on.
"""

from pathlib import Path

_HERE = Path(__file__).resolve().parent
_OWN_JOB = (_HERE / "install", _HERE / "system")


def pytest_ignore_collect(collection_path, config):
    path = Path(collection_path).resolve()
    for suite in _OWN_JOB:
        if path == suite or suite in path.parents:
            named = (Path(arg.split("::")[0]).resolve() for arg in config.args)
            return not any(a == suite or suite in a.parents for a in named)
    return None
