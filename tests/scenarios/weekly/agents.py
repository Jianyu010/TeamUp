"""Agents: real opencode sessions, or scripted stand-ins for --dry-run.

Both kinds expose the same `turn()` and return the same `Turn` record, so the
orchestrator and the transcripts do not care which one ran.
"""

import asyncio
import json
import os
import re
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .stack import ROOT, Developer

PROVIDER = "csi-provider"
DEFAULT_MODEL = "DeepSeek-V4.1-Flash-1017"
DEFAULT_BASE_URL = "http://127.0.0.1:18081/v1"
SERVER = "tu"  # MCP server name inside opencode; tools appear as tu_teamup_*
SKILL = ROOT / "plugin/skills/teamup-collaboration/SKILL.md"


@dataclass
class Spec:
    """Who an agent is. `facts` is private knowledge it shares only when asked.

    `mention` says whom a wake reply @mentions: "sender", an agent name, or None.
    """

    name: str
    role: str
    facts: list[str] = field(default_factory=list)
    mention: str | None = None
    wake_note: str = ""


@dataclass
class Call:
    name: str  # e.g. teamup_send_message
    input: dict
    output: object
    started: float
    ended: float
    error: bool = False


@dataclass
class Turn:
    agent: str
    kind: str  # setup | wake | nudge
    prompt: str
    started: float
    ended: float = 0.0
    steps: int = 0
    tokens: dict = field(default_factory=lambda: {"input": 0, "output": 0, "total": 0})
    calls: list[Call] = field(default_factory=list)
    text: str = ""
    error: str = ""
    session_id: str = ""

    def sent_times(self) -> list[float]:
        return [
            call.ended
            for call in self.calls
            if call.name == "teamup_send_message" and not call.error
        ]

    def output_of(self, name: str) -> list[dict]:
        return [
            parsed
            for call in self.calls
            if call.name == name
            and not call.error
            and (parsed := first_json(call.output)) is not None
        ]


def first_json(value) -> dict | None:
    """The first JSON object in a tool result (the MCP adds a hint line)."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    start = value.find("{")
    if start < 0:
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(value[start:])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


class Scrubber:
    def __init__(self, secrets: list[str]):
        self.secrets = [secret for secret in secrets if secret]

    def __call__(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "<redacted>")
            return value
        if isinstance(value, dict):
            return {key: self(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self(item) for item in value]
        return value


def write_transcript(path: Path, turn: Turn, scrub: Scrubber) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "agent": turn.agent,
        "kind": turn.kind,
        "started": round(turn.started, 3),
        "seconds": round(turn.ended - turn.started, 1),
        "steps": turn.steps,
        "tokens": turn.tokens,
        "session": turn.session_id,
        "error": turn.error,
        "prompt": turn.prompt,
        "calls": [
            {
                "tool": call.name,
                "input": call.input,
                "output": call.output,
                "error": call.error,
            }
            for call in turn.calls
        ],
        "text": turn.text,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(scrub(record), ensure_ascii=False) + "\n")


# --- opencode ---------------------------------------------------------------


def find_opencode() -> str:
    candidates = [os.environ.get("TEAMUP_OPENCODE")]
    home = Path.home()
    candidates += [
        home / "opencode-review/opencode/node_modules/opencode-linux-x64/bin/opencode",
        home / "opencode-review/opencode/node_modules/.bin/opencode",
        "/snap/bin/opencode",
        shutil.which("opencode"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise FileNotFoundError(
        "opencode not found; set TEAMUP_OPENCODE to the binary path"
    )


def skill_text() -> str:
    text = SKILL.read_text(encoding="utf-8")
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL).strip()


def system_prompt(spec: Spec) -> str:
    facts = "\n".join(f"- {fact}" for fact in spec.facts) or "- (none)"
    return f"""You are "{spec.name}", one AI agent in a small team. The team coordinates only through TeamUp, a message service for agents. Your only tools are the TeamUp tools (names start with `{SERVER}_teamup_`). You have no shell, files or web.

Your TeamUp role description is exactly: {spec.role}

Private knowledge you hold (tell teammates only when they ask, in a TeamUp message):
{facts}

Working rules:
- Each user message is one task. Do what it asks with the TeamUp tools, then give a one-line final answer and stop. Do not poll, wait or repeat finished work.
- Copy IDs (room_id, member_id, version) from tool results. Never invent them.
- Give every new message a unique client_message_id, for example "{spec.name.lower()}-<purpose>-<random digits>".
- Keep messages short and factual.
- Your owner has approved every TeamUp tool call, so do not ask for permission.

{skill_text()}
"""


def opencode_config(dev: Developer, spec: Spec, model: str, base_url: str) -> dict:
    allow = {"*": False, f"{SERVER}_*": True}
    return {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "model": f"{PROVIDER}/{model}",
        "default_agent": "teamup",
        # The relay injects the API key; no key is configured or read here.
        "provider": {
            PROVIDER: {
                "name": "CSI via local relay",
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": base_url, "timeout": 300000},
                "models": {model: {"name": model}},
            }
        },
        "mcp": {
            SERVER: {
                "type": "local",
                "command": [sys.executable, "-m", "teamup_mcp.main"],
                "environment": dev.mcp_environment(),
                "enabled": True,
                "timeout": 30000,
            }
        },
        "permission": {"*": "deny", f"{SERVER}_*": "allow"},
        "tools": allow,
        "agent": {
            "teamup": {
                "mode": "primary",
                "description": f"TeamUp agent {spec.name}",
                "prompt": system_prompt(spec),
                "temperature": 0.2,
                "permission": {"*": "deny", f"{SERVER}_*": "allow"},
                "tools": allow,
            }
        },
    }


class OpencodeAgent:
    """One opencode session per agent, resumed with `opencode run --session`."""

    def __init__(self, spec: Spec, dev: Developer, root: Path, options: dict):
        self.spec, self.dev, self.options = spec, dev, options
        self.session_id = ""
        self.binary = find_opencode()
        base = root / f"oc-{spec.name}"
        (base / "config/opencode").mkdir(parents=True)
        (base / "data").mkdir()
        (base / "work").mkdir()
        self.base = base
        config = opencode_config(dev, spec, options["model"], options["base_url"])
        path = base / "config/opencode/opencode.json"
        path.touch(mode=0o600)
        path.write_text(json.dumps(config, indent=2))

    def environment(self) -> dict[str, str]:
        env = {
            k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TZ") if k in os.environ
        }
        env["XDG_CONFIG_HOME"] = str(self.base / "config")
        env["XDG_DATA_HOME"] = str(self.base / "data")
        return env

    async def turn(self, kind: str, prompt: str, task=None) -> Turn:
        turn = Turn(self.spec.name, kind, prompt, time.time())
        command = [
            self.binary,
            "run",
            "--pure",
            "--format",
            "json",
            "--agent",
            "teamup",
        ]
        if self.session_id:
            command += ["--session", self.session_id]
        command.append(prompt)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.base / "work",
            env=self.environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(
                process.communicate(), self.options["turn_timeout"]
            )
        except (TimeoutError, asyncio.CancelledError) as stop:
            with_group(process, signal.SIGKILL)
            await process.wait()
            if isinstance(stop, asyncio.CancelledError):
                raise
            turn.error = f"turn exceeded {self.options['turn_timeout']}s"
            out, err = b"", b""
        turn.ended = time.time()
        parse_events(turn, out.decode(errors="replace"))
        if not turn.error and process.returncode:
            turn.error = (
                f"opencode exited {process.returncode}: "
                + err.decode(errors="replace").strip()[-400:]
            )
        if not self.session_id:
            self.session_id = turn.session_id
        return turn


def with_group(process, sig) -> None:
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def parse_events(turn: Turn, stdout: str) -> None:
    """Fold opencode `--format json` events into a Turn."""
    texts = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        turn.session_id = event.get("sessionID") or turn.session_id
        part = event.get("part") or {}
        kind = event.get("type")
        if kind == "text":
            texts.append(part.get("text", ""))
        elif kind == "step_finish":
            turn.steps += 1
            tokens = part.get("tokens") or {}
            for key in ("input", "output", "total"):
                turn.tokens[key] += int(tokens.get(key) or 0)
        elif kind == "tool_use":
            state = part.get("state") or {}
            when = state.get("time") or {}
            name = str(part.get("tool", ""))
            name = name.removeprefix(f"{SERVER}_")
            failed = state.get("status") == "error"
            turn.calls.append(
                Call(
                    name,
                    state.get("input") or {},
                    state.get("output") if not failed else state.get("error"),
                    (when.get("start") or event.get("timestamp") or 0) / 1000,
                    (when.get("end") or event.get("timestamp") or 0) / 1000,
                    failed,
                )
            )
        elif kind == "error":
            data = (event.get("error") or {}).get("data") or {}
            turn.error = (data.get("message") or json.dumps(event.get("error")))[:400]
    turn.text = "\n".join(texts).strip()


# --- scripted stand-in --------------------------------------------------------


class ScriptContext:
    """What a scripted agent step sees: its MCP session and bookkeeping."""

    def __init__(self, agent, session, turn: Turn):
        self.agent, self.session, self.turn = agent, session, turn
        self.spec = agent.spec

    async def call(self, operation: str, **arguments):
        started = time.time()
        response = await self.session.call_tool(f"teamup_{operation}", arguments)
        text = response.content[0].text if response.content else ""
        self.turn.calls.append(
            Call(
                f"teamup_{operation}",
                arguments,
                text,
                started,
                time.time(),
                bool(response.isError),
            )
        )
        if response.isError:
            raise RuntimeError(f"{operation}: {text}")
        return response.structuredContent

    def message_id(self, purpose: str) -> str:
        self.agent.counter += 1
        return f"{self.spec.name.lower()}-{purpose}-{self.agent.counter}"


class ScriptedAgent:
    """Zero-model agent for --dry-run: runs a Python script through real MCP."""

    def __init__(self, spec: Spec, dev: Developer):
        self.spec, self.dev = spec, dev
        self.session_id = f"scripted-{spec.name}"
        self.counter = 0

    async def turn(self, kind: str, prompt: str, task=None) -> Turn:
        turn = Turn(
            self.spec.name, kind, prompt, time.time(), session_id=self.session_id
        )
        try:
            async with self.dev.session() as session:
                await task.script(ScriptContext(self, session, turn))
        except Exception as error:  # noqa: BLE001 - a failed turn is data
            turn.error = str(error)[:400]
        turn.ended = time.time()
        return turn
