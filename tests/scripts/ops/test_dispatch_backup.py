import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ops.dispatch_backup import capture_command

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/ops/dispatch_backup.py"


def test_capture_runs_as_configured_database_owner(tmp_path):
    config = tmp_path / "sources.json"
    config.write_text(
        json.dumps({"pilot": {"owner": "teamup", "database": "/srv/pilot.db"}})
    )

    assert capture_command(config, "pilot") == [
        "/usr/bin/sudo",
        "-n",
        "-u",
        "teamup",
        "--",
        "/usr/bin/python3",
        str(SCRIPT.with_name("capture_backup.py")),
        "--source",
        "/srv/pilot.db",
    ]


@pytest.mark.parametrize(
    "requested_source", ["", "pilot ; id", "pilot --source /private", "unknown"]
)
def test_only_an_exact_configured_source_is_accepted(tmp_path, requested_source):
    config = tmp_path / "sources.json"
    config.write_text(
        json.dumps({"pilot": {"owner": "teamup", "database": "/srv/private.db"}})
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(config)],
        env={**os.environ, "SSH_ORIGINAL_COMMAND": requested_source},
        check=False,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode != 0
    assert result.stdout == b""
    assert result.stderr == b"Backup request denied.\n"
