"""Enter the installed WSL Client runtime through the existing desktop launcher."""

import argparse
import os
import sys
from pathlib import Path


def absolute_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("TeamUp runtime and profile paths must be absolute WSL paths.")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("launcher", "runtime", "config", "state-dir"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    try:
        runtime = absolute_path(args.runtime)
        launcher = absolute_path(args.launcher)
        profile = absolute_path(args.config)
        state = absolute_path(args.state_dir)
        python, client = runtime / "python", runtime / "teamup-client"
        if not all(
            path.is_file() and os.access(path, os.X_OK) for path in (python, client)
        ):
            raise ValueError(
                "Install the TeamUp Client runtime in WSL before opening TeamUp."
            )
        os.execv(
            python,
            [
                str(python),
                "-I",
                str(launcher),
                "--config",
                str(profile),
                "--client",
                str(client),
                "--state-dir",
                str(state),
            ],
        )
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
