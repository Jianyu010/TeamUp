"""Collect scenario suites only when TEAMUP_SCENARIOS names them.

The PR gate runs plain ``pytest`` without the variable, so every suite
directory is ignored and no scenario is collected. Nightly sets
``TEAMUP_SCENARIOS=nightly``. See docs/ci/contract.md.
"""

import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SUITES = ("nightly", "perf", "weekly")
_ENABLED = {
    name.strip()
    for name in os.environ.get("TEAMUP_SCENARIOS", "").split(",")
    if name.strip()
}
collect_ignore_glob = [f"{suite}/*" for suite in _SUITES if suite not in _ENABLED]

# The suites import _stack and _results as top-level modules.
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_records = {}
_session_start = None


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "scenario(id, title): catalogue ID and title for the results file"
    )


def pytest_sessionstart(session):
    global _session_start
    import _results

    _session_start = _results.utc_now()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    setattr(item, f"rep_{report.when}", report)
    mark = item.get_closest_marker("scenario")
    if mark is None or len(mark.args) < 2:
        return report
    record = _records.setdefault(
        item.nodeid,
        {
            "id": mark.args[0],
            "title": mark.args[1],
            "suite": item.path.parent.name,
            "status": "pass",
            "detail": "",
            "duration_s": 0.0,
        },
    )
    record["duration_s"] += report.duration
    for key, value in report.user_properties:
        if key == "detail" and record["status"] == "pass":
            record["detail"] = str(value)
    # Keep the most severe outcome across setup, call and teardown.
    if report.skipped:
        if hasattr(report, "wasxfail"):
            record.update(
                status="skip", detail=f"known issue (xfail): {report.wasxfail}"
            )
        elif record["status"] == "pass":
            reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else ""
            record.update(status="skip", detail=str(reason))
    elif report.failed:
        if record["status"] != "fail":
            record.update(
                status="fail" if report.when == "call" else "error",
                detail=report.longreprtext[-1800:],
            )
    elif report.passed and report.when == "call" and hasattr(report, "wasxfail"):
        record.update(status="fail", detail="xfail(strict) test unexpectedly passed")
    return report


def pytest_sessionfinish(session, exitstatus):
    if not _records:
        return
    import _results

    suites = {}
    for record in _records.values():
        suites.setdefault(record["suite"], []).append(
            _results.scenario_entry(
                record["id"],
                record["title"],
                record["status"],
                record["duration_s"],
                record["detail"],
            )
        )
    for suite, entries in suites.items():
        entries.sort(key=lambda entry: entry["id"])
        doc = _results.document(suite, _session_start, entries, [])
        _results.write(_results.results_dir() / suite / "scenarios.json", doc)


@pytest.fixture
def ws(request):
    """A short /tmp workspace; its processes are always stopped, logs shown on failure."""
    from _stack import Workspace

    workspace = Workspace()
    yield workspace
    call = getattr(request.node, "rep_call", None)
    if call is not None and call.failed:
        sys.stderr.write(workspace.logs())
    workspace.close()


@pytest.fixture(scope="session")
def workspace_venv():
    """Bin directory of one venv with all three packages installed from local paths."""
    import shutil
    import tempfile

    from _stack import install_venv

    root = Path(tempfile.mkdtemp(prefix="tu-venv-", dir="/tmp"))
    try:
        yield install_venv(root / "all", ["server", "client", "plugin/mcp"])
    finally:
        shutil.rmtree(root, ignore_errors=True)
