import sys
from pathlib import Path

# _stack is a plain module next to the scenarios; it imports no TeamUp internals.
_SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
if str(_SCENARIOS) not in sys.path:
    sys.path.insert(0, str(_SCENARIOS))
