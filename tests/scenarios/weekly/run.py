"""Orchestrator: runs agent turns, plays the wake, judges from TeamUp state.

The TeamUp Client has no opencode wake driver (only claude-code and codex), so
the orchestrator plays the wake. It watches each agent's Client through the
desktop-notice recorder, a public Client surface that does not mark anything
read, and starts that agent's next opencode turn when a new notice appears.
The real Claude Code and Codex wake drivers are therefore NOT exercised here.
"""

import asyncio
import time
from pathlib import Path

from .agents import OpencodeAgent, ScriptedAgent, Scrubber, Spec, Turn, write_transcript
from .stack import Stack, call
from .tasks import Task, wake


class Failure(Exception):
    """The scenario ran but its outcome is wrong."""


class BudgetExceeded(Failure):
    pass


class Run:
    def __init__(self, scenario_id: str, specs: list[Spec], options: dict):
        self.id = scenario_id
        self.specs = {spec.name: spec for spec in specs}
        self.options = options
        self.max_turns = options["max_turns"]
        self.deadline = time.monotonic() + options["max_seconds"]
        self.stack = Stack(list(self.specs))
        self.agents: dict = {}
        self.turns: list[Turn] = []
        self.started_turns = 0
        self.handled = {name: 0 for name in self.specs}
        self.roster = {spec.name: spec.role for spec in specs}
        self.scrub = Scrubber([])
        self.transcripts = Path(options["transcripts"])

    async def __aenter__(self):
        await self.stack.start()
        self.scrub = Scrubber(self.stack.secrets)
        for name, spec in self.specs.items():
            dev = self.stack.developers[name]
            if self.options["dry_run"]:
                self.agents[name] = ScriptedAgent(spec, dev)
            else:
                self.agents[name] = OpencodeAgent(
                    spec, dev, self.stack.root, self.options
                )
        self.started = time.time()
        return self

    async def __aexit__(self, *exc):
        self.elapsed = time.time() - self.started
        self.options["collector"][self.id] = self
        self.stack.stop()

    # --- turns ----------------------------------------------------------------

    async def turn(self, name: str, task: Task, kind: str = "setup") -> Turn:
        if self.started_turns >= self.max_turns:
            raise BudgetExceeded(f"turn cap of {self.max_turns} reached")
        if time.monotonic() > self.deadline:
            raise BudgetExceeded(
                f"wall-time cap of {self.options['max_seconds']}s reached"
            )
        self.started_turns += 1
        result = await self.agents[name].turn(kind, task.prompt, task)
        self.turns.append(result)
        path = self.transcripts / f"{self.id}-{name}.jsonl"
        write_transcript(path, result, self.scrub)
        return result

    async def phase(self, *work: tuple[str, Task]) -> list[Turn]:
        """Run these agents' tasks at the same time."""
        return list(await asyncio.gather(*(self.turn(n, t) for n, t in work)))

    # --- reading state through the public MCP surface -------------------------

    def room_id(self) -> str:
        return self._created_id("teamup_create_room", "room_id")

    def linked_room_id(self) -> str:
        return self._created_id("teamup_create_linked_room", "room_id")

    async def eventually(self, probe, what: str, timeout: float = 20.0):
        """Poll a state read until it returns a truthy value."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if value := await probe():
                return value
            await asyncio.sleep(0.2)
        raise Failure(f"Timed out waiting for {what}. Errors: {self.turn_errors()}")

    def dm_id(self) -> str:
        return self._created_id("teamup_open_dm", "dm_id")

    def _created_id(self, operation: str, field: str) -> str:
        for turn in self.turns:
            for output in turn.output_of(operation):
                if isinstance(output.get(field), str):
                    return output[field]
        raise Failure(f"{operation} returned no {field}. Errors: {self.turn_errors()}")

    def member_id(self, name: str) -> str:
        for turn in self.turns:
            if turn.agent == name:
                for output in turn.output_of("teamup_register"):
                    if isinstance(output.get("member_id"), str):
                        return output["member_id"]
        raise Failure(f"{name} never registered")

    async def messages(self, room_id: str, reader: str) -> list[dict]:
        """Whole room history as one member sees it (the room creator sees all)."""
        return (await self.read(reader, "read_messages", room_id=room_id, limit=100))[
            "messages"
        ]

    async def dm_messages(self, dm_id: str, reader: str) -> list[dict]:
        """Read the DM as one of its participants."""
        return (await self.read(reader, "read_dm", dm_id=dm_id, limit=100))["messages"]

    async def read(self, reader: str, operation: str, **arguments) -> dict:
        async with self.stack.developers[reader].session() as session:
            failed, result = await call(session, operation, **arguments)
        if failed:
            raise Failure(f"{reader} could not {operation}: {result}")
        return result

    async def has_acked(self, name: str, room_id: str) -> bool:
        """TeamUp refuses reads until the member acked the current board."""
        async with self.stack.developers[name].session() as session:
            failed, result = await call(
                session, "read_messages", room_id=room_id, limit=1
            )
        if failed and "board_ack_required" not in result:
            raise Failure(f"{name} read failed: {result}")
        return not failed

    # --- the wake ---------------------------------------------------------------

    async def drain(self, done, watch=None, settle: float = 4.0) -> None:
        """Wake agents on new notices until `done()` returns (True, _).

        `done` is an async callable returning (finished, why_not). Stops early on
        success once started turns finish. Raises Failure otherwise.
        """
        watch = list(watch or self.specs)
        running: dict[str, asyncio.Task] = {}
        idle = 0.0
        last_check = 0.0
        finished_turn = True
        why = "not checked"
        finished = False
        try:
            while True:
                if finished_turn or not running or time.monotonic() - last_check > 5:
                    ok, why = await done()
                    last_check, finished_turn = time.monotonic(), False
                    if ok:
                        finished = True
                        return
                for name in watch:
                    count = len(self.stack.developers[name].notices())
                    if name not in running and count > self.handled[name]:
                        self.handled[name] = count
                        task = wake(self.specs[name], count, self.roster)
                        running[name] = asyncio.create_task(
                            self.turn(name, task, "wake")
                        )
                if running:
                    idle = 0.0
                    await asyncio.wait(
                        running.values(), timeout=1, return_when=asyncio.FIRST_COMPLETED
                    )
                    for name in [n for n, t in running.items() if t.done()]:
                        running.pop(name).result()
                        finished_turn = True
                else:
                    idle += 0.5
                    if idle >= settle:
                        ok, why = await done()
                        if ok:
                            finished = True
                            return
                        raise Failure(f"agents went quiet without finishing: {why}")
                    await asyncio.sleep(0.5)
        finally:
            # On success let started turns finish so transcripts and metrics are
            # whole; no new wake starts. On failure stop them.
            if not finished:
                for task in running.values():
                    task.cancel()
            await asyncio.gather(*running.values(), return_exceptions=True)

    def turn_errors(self) -> str:
        return "; ".join(
            f"{t.agent}/{t.kind}: {t.error}" for t in self.turns if t.error
        )
