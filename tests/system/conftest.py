import sys
from pathlib import Path

import pytest

# _stack is a plain module next to the scenarios; it imports no TeamUp internals.
_SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
if str(_SCENARIOS) not in sys.path:
    sys.path.insert(0, str(_SCENARIOS))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    setattr(item, f"rep_{report.when}", report)
    return report


@pytest.fixture
def ws(request):
    """A short /tmp workspace; its processes are always stopped, logs shown on failure."""
    from _stack import Workspace

    workspace = Workspace()
    yield workspace
    failed = getattr(request.node, "rep_call", None)
    if failed is not None and failed.failed:
        sys.stderr.write(workspace.logs())
    workspace.close()
