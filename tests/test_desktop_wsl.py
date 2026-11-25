"""The Windows bridge runs in WSL; exercise its actual exec into a runtime."""

import json
import subprocess
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1] / "client/desktop/launch-wsl.py"


def test_bridge_enters_selected_runtime_without_shell_interpolation(tmp_path):
    runtime = tmp_path / "runtime with spaces"
    runtime.mkdir()
    (runtime / "python").symlink_to(sys.executable)
    client = runtime / "teamup-client"
    client.write_text("unused")
    client.chmod(0o700)
    launcher = tmp_path / "launch.py"
    launcher.write_text("import json,sys;print(json.dumps(sys.argv[1:]))")
    profile = tmp_path / "literal $(nothing); profile.json"
    state = tmp_path / "launcher state"
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(BRIDGE),
            "--launcher",
            str(launcher),
            "--runtime",
            str(runtime),
            "--config",
            str(profile),
            "--state-dir",
            str(state),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(completed.stdout) == [
        "--config",
        str(profile),
        "--client",
        str(client),
        "--state-dir",
        str(state),
    ]


def test_bridge_rejects_missing_runtime_without_printing_a_launch_credential(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(BRIDGE),
            "--launcher",
            str(tmp_path / "launch.py"),
            "--runtime",
            str(tmp_path / "missing"),
            "--config",
            str(tmp_path / "client.json"),
            "--state-dir",
            str(tmp_path / "state"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "Install the TeamUp Client runtime in WSL" in completed.stderr


def test_bridge_rejects_relative_wsl_configuration_paths(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(BRIDGE),
            "--launcher",
            str(tmp_path / "launch.py"),
            "--runtime",
            "relative/runtime",
            "--config",
            str(tmp_path / "client.json"),
            "--state-dir",
            str(tmp_path / "state"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "absolute WSL paths" in completed.stderr
    assert completed.stdout == ""
