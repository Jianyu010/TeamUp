"""Durable room command submission for an approved member binding."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Protocol
from uuid import uuid4
from weakref import WeakValueDictionary

from ._protocol import is_integer, is_string, is_uuid
from ._room_store import CommandScope, RoomStore, StoredCommand
from ._upstream import Request, unavailable
from .ipc import ClientError

KINDS = {
    "create_room": "create_room",
    "join_room": "join_room",
    "leave_room": "leave_room",
    "remove_room_member": "remove_member",
    "publish_board": "publish_board",
    "ack_board": "acknowledge_board",
    "send_message": "send_message",
    "create_task": "create_task",
    "join_task": "join_task",
    "report_progress": "record_task_progress",
    "finish_task": "finish_task",
}
RETRY_KEYS = {
    "send_message": "client_message_id",
    "create_task": "client_task_id",
    "record_task_progress": "client_progress_id",
}
TASKS = {"create_task", "join_task", "record_task_progress", "finish_task"}
ACCESS_LOST = {
    "membership_changed",
    "member_identity_changed",
    "member_retired",
    "room_archived",
    "server_generation_changed",
}
REVIEWABLE_REJECTIONS = {"board_ack_required", "board_read_required", "conflict"}
RECONNECT_LIMIT = 16


class Binding(Protocol):
    @property
    def server_profile(self) -> str: ...

    @property
    def account_id(self) -> str: ...

    @property
    def member_id(self) -> str: ...

    @property
    def token(self) -> str: ...


@dataclass(frozen=True)
class Membership:
    identifier: str | None
    revision: int


def _fingerprint(value) -> str:
    wire = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(wire.encode()).hexdigest()


def _intent_parameters(kind: str, supplied: dict) -> dict:
    fields = {key: value for key, value in supplied.items() if key != "room_id"}
    for key in ("member_id", "task_id"):
        if key in fields:
            fields[key] = fields[key].lower()
    if kind == "send_message":
        primary = fields.pop("mention_member_id", None)
        targets = fields.pop("mention_member_ids", None)
        if primary is not None and targets is not None:
            raise ClientError("invalid_request", "Provide one form of mention targets")
        targets = [
            member.lower() for member in (targets or ([primary] if primary else []))
        ]
        if len(targets) == 1:
            fields["mention_member_id"] = targets[0]
        elif targets:
            fields["mention_member_ids"] = targets
    elif kind == "create_task":
        addresses = fields.pop("addressed_member_ids", None)
        if addresses:
            fields["addressed_member_ids"] = sorted(
                member.lower() for member in addresses
            )
        if fields.get("description") is None:
            fields.pop("description", None)
        assignment = fields.pop("assignment", None)
        if assignment is not None:
            fields["assignment"] = {
                key: value for key, value in assignment.items() if value is not None
            }
    elif kind == "create_room":
        fields.setdefault("history_policy", "since_joining")
    elif kind == "leave_room" and fields.get("notify") is False:
        fields.pop("notify")
    return fields


def _blocked(command: StoredCommand) -> ClientError:
    if command.receipt is not None:
        return _result_unavailable()
    if command.attempts:
        return ClientError(
            "command_outcome_unknown", "The room command outcome is unknown"
        )
    return ClientError(
        "command_blocked", "The room command was blocked before dispatch"
    )


def _result_unavailable() -> ClientError:
    return ClientError(
        "command_result_unavailable",
        "The command committed but its result is unavailable",
    )


class RoomCommands[ActiveBinding: Binding]:
    def __init__(
        self,
        store: RoomStore,
        request: Request,
        binding_current: Callable[[ActiveBinding], bool],
    ):
        self._store = store
        self._request = request
        self._binding_current = binding_current
        self._queues: WeakValueDictionary[CommandScope, asyncio.Lock] = (
            WeakValueDictionary()
        )

    @staticmethod
    def supports(method: str) -> bool:
        return method in KINDS

    def invalidate_member(
        self, server_profile: str, account_id: str, member_id: str
    ) -> None:
        self._store.invalidate_commands(server_profile, account_id, member_id)

    def invalidate_room(
        self,
        server_profile: str,
        account_id: str,
        member_id: str,
        room_id: str,
        *,
        membership_id: str | None = None,
    ) -> None:
        self._store.invalidate_commands(
            server_profile,
            account_id,
            member_id,
            room_id=room_id,
            membership_id=membership_id,
        )

    def _queue_lock(self, scope: CommandScope) -> asyncio.Lock:
        lock = self._queues.get(scope)
        if lock is None:
            lock = self._queues[scope] = asyncio.Lock()
        return lock

    async def execute(
        self, binding: ActiveBinding, method: str, params: dict
    ) -> dict | None:
        kind = KINDS.get(method)
        if kind is None:
            return None
        room_id = params.get("room_id", "").lower()
        scope = CommandScope(
            binding.server_profile, binding.account_id, binding.member_id, room_id
        )
        fields = _intent_parameters(kind, params)
        retry_field = RETRY_KEYS.get(kind)
        caller_key = fields[retry_field] if retry_field is not None else None
        if kind in {"join_room", "leave_room"}:
            caller_key = fields.pop("client_request_id", None)
        retry_key = _fingerprint(caller_key) if caller_key is not None else None
        intent = _fingerprint([room_id, fields])
        async with self._queue_lock(scope):
            return await self._submit(binding, scope, kind, fields, intent, retry_key)

    async def retry_pending(self, binding: ActiveBinding) -> None:
        """On reconnection, send each eligible durable intent at most once automatically."""
        actor = CommandScope(
            binding.server_profile, binding.account_id, binding.member_id, ""
        )
        queue = self._store.reconnect_queue(actor, RECONNECT_LIMIT)
        if not queue:
            return
        try:
            generation, supported = await self._capabilities(binding)
        except ClientError:
            return
        if generation is None:
            return
        for room_id, operation_id in queue:
            if not self._binding_current(binding):
                return
            scope = replace(actor, room_id=room_id)
            async with self._queue_lock(scope):
                command = self._store.pending_command(scope)
                if (
                    command is None
                    or command.request is None
                    or command.operation_id != operation_id
                ):
                    continue
                kind = command.request["kind"]
                if kind not in supported:
                    continue
                try:
                    membership = (
                        await self._membership(binding, room_id, generation)
                        if room_id
                        else Membership(None, 0)
                    )
                    if command.generation != generation or not self._still_eligible(
                        command, kind, membership
                    ):
                        self._store.block_command(command.operation_id, redact=True)
                        continue
                    await self._dispatch(binding, command, automatic=True)
                except ClientError as exc:
                    if exc.code in {"room_archived", "membership_changed", "not_found"}:
                        self._store.block_command(command.operation_id, redact=True)
                    if exc.code in {
                        "unavailable",
                        "member_identity_changed",
                        "server_generation_changed",
                    }:
                        return

    async def _submit(
        self,
        binding: ActiveBinding,
        scope: CommandScope,
        kind: str,
        fields: dict,
        intent: str,
        retry_key: str | None,
    ) -> dict | None:
        self._check_binding(binding)
        command = self._store.find_command(scope, kind, retry_key, intent)
        if command is not None:
            if command.intent != intent:
                raise ClientError(
                    "conflict",
                    "The room command retry key refers to a different request",
                )
            if command.blocked:
                raise _blocked(command)
        pending = self._store.pending_command(scope)
        try:
            generation, commands = await self._capabilities(binding)
        except ClientError as exc:
            if command is not None:
                self._block_if_denied(command, exc)
                raise _blocked(command) from None
            raise
        if kind not in commands or generation is None:
            if command is not None:
                raise _blocked(command)
            if pending is not None:
                raise ClientError(
                    "command_pending", "The room has an unresolved command"
                )
            return None
        membership = Membership(None, 0)
        if scope.room_id:
            try:
                membership = await self._membership(binding, scope.room_id, generation)
            except ClientError as exc:
                if exc.code in {"room_archived", "not_found"}:
                    self.invalidate_room(
                        scope.server_profile,
                        scope.account_id,
                        scope.member_id,
                        scope.room_id,
                    )
                if command is not None:
                    # A disagreement between reads leaves the current generation
                    # unconfirmed. The captured request still cannot be dispatched.
                    if exc.code != "server_generation_changed":
                        self._block_if_denied(command, exc)
                    raise _blocked(command) from None
                if kind == "leave_room" and exc.code == "not_found":
                    return self._leave_without_membership(
                        scope, generation, fields, retry_key, intent
                    )
                raise
        if command is not None:
            if command.generation != generation or not self._still_eligible(
                command, kind, membership
            ):
                self._store.block_command(command.operation_id, redact=True)
                raise _blocked(command)
        else:
            if kind == "leave_room" and membership.identifier is None:
                return self._leave_without_membership(
                    scope, generation, fields, retry_key, intent
                )
            metadata = (
                await self._room_metadata(binding, scope.room_id, membership)
                if membership.identifier
                else {}
            )
            if pending is not None:
                if pending.generation != generation:
                    self._store.block_command(pending.operation_id, redact=True)
                else:
                    self._check_queue(pending, membership, metadata)
            parameters = self._capture(kind, fields, membership, metadata)
            body = {
                "operation_id": str(uuid4()),
                "expected_member_id": binding.member_id,
                "server_generation": generation,
                "kind": kind,
                **({"room_id": scope.room_id} if scope.room_id else {}),
                "parameters": parameters,
            }
            command = self._store.stage_command(
                scope, generation, membership.identifier, retry_key, intent, body
            )
            if command is None:
                raise ClientError(
                    "command_pending", "The room has an unresolved command"
                )
        receipt = command.receipt
        if receipt is None:
            receipt = await self._dispatch(binding, command)
        self._check_binding(binding)
        if not self._store.command_result_current(command.operation_id):
            raise _result_unavailable()
        try:
            result = await self._hydrate(
                binding, generation, command, kind, fields, receipt
            )
        except ClientError as exc:
            if exc.code in ACCESS_LOST:
                self._store.block_command(command.operation_id, redact=True)
            raise _result_unavailable() from None
        self._check_binding(binding)
        if not self._store.release_command_result(command.operation_id):
            raise _result_unavailable()
        return result

    def _check_binding(self, binding: ActiveBinding) -> None:
        if not self._binding_current(binding):
            raise ClientError("not_found", "Local session is unavailable")

    async def _read(
        self, binding: ActiveBinding, path: str, query: dict | None = None
    ) -> dict:
        self._check_binding(binding)
        result = await self._request("GET", path, binding.token, query=query)
        self._check_binding(binding)
        return result

    async def _capabilities(self, binding: ActiveBinding) -> tuple[str | None, list]:
        try:
            result = await self._read(binding, "/sync/capabilities")
        except ClientError as exc:
            if exc.code != "not_found":
                raise
            self._check_binding(binding)
            return None, []
        generation = result.get("server_generation")
        commands = result.get("commands", [])
        if (
            not is_integer(result.get("version"), 1, 1)
            or not isinstance(generation, str)
            or not is_string(generation, 1024)
            or not isinstance(commands, list)
            or not all(is_string(kind, 64) for kind in commands)
        ):
            raise unavailable()
        return generation, commands

    async def _membership(
        self, binding: ActiveBinding, room_id: str, generation: str
    ) -> Membership:
        result = await self._read(
            binding,
            f"/rooms/{room_id}/membership-state",
            {"expected_member_id": binding.member_id},
        )
        if result.get("server_generation") != generation:
            raise ClientError(
                "server_generation_changed", "The Server generation changed"
            )
        if result.get("member_id") != binding.member_id:
            raise ClientError(
                "member_identity_changed", "The registered member changed"
            )
        membership = result.get("membership_id")
        revision = result.get("membership_revision")
        if (
            result.get("room_id") != room_id
            or "membership_id" not in result
            or (membership is not None and not is_string(membership, 128))
            or type(revision) is not int
            or revision < 0
        ):
            raise unavailable()
        return Membership(membership, revision)

    async def _room_metadata(
        self, binding: ActiveBinding, room_id: str, membership: Membership
    ) -> dict:
        listing = await self._read(binding, "/rooms", {"include_membership": "true"})
        rooms = listing.get("rooms")
        if not isinstance(rooms, list):
            raise unavailable()
        for room in rooms:
            if isinstance(room, dict) and room.get("room_id") == room_id:
                if room.get("membership_id") != membership.identifier:
                    raise ClientError(
                        "membership_changed", "The room membership changed"
                    )
                return room
        raise ClientError("membership_changed", "The room membership changed")

    @staticmethod
    def _still_eligible(
        command: StoredCommand, kind: str, membership: Membership
    ) -> bool:
        if command.receipt is not None and kind == "join_room":
            return membership.identifier == command.receipt.get("membership_id")
        if kind == "join_room":
            expected_revision = (
                command.request["parameters"]["membership_revision"]
                if command.request
                else -1
            )
            if (
                command.attempts
                and command.membership_id is None
                and membership.identifier
                and membership.revision == expected_revision + 1
            ):
                return True  # An exact receipt may confirm the join.
            return (
                membership.revision == expected_revision
                and membership.identifier == command.membership_id
            )
        if kind == "leave_room" and command.attempts and membership.identifier is None:
            return True  # An exact leave receipt can be recovered with no membership.
        return membership.identifier == command.membership_id

    def _check_queue(
        self, pending: StoredCommand, membership: Membership, metadata: dict
    ) -> None:
        if pending.request is None:
            raise _blocked(pending)
        kind = pending.request["kind"]
        if not self._still_eligible(pending, kind, membership):
            self._store.block_command(pending.operation_id, redact=True)
            return
        parameters = pending.request["parameters"]
        version = parameters.get("expected_board_version", parameters.get("version"))
        if version is not None and metadata.get("current_board_version") != version:
            self._store.block_command(pending.operation_id)
            return
        raise ClientError("command_pending", "The room has an unresolved command")

    @staticmethod
    def _capture(
        kind: str, fields: dict, membership: Membership, metadata: dict
    ) -> dict:
        parameters = fields.copy()
        if kind == "create_room":
            return parameters
        if kind == "join_room":
            return {**parameters, "membership_revision": membership.revision}
        if kind == "leave_room":
            selected = parameters.pop("membership_id", None)
            if selected is not None and selected != membership.identifier:
                raise ClientError("membership_changed", "The room membership changed")
        if kind == "remove_member":
            parameters["target_member_id"] = parameters.pop("member_id")
            parameters["target_membership_id"] = parameters.pop("membership_id")
        if membership.identifier is None:
            raise ClientError("not_found", "Resource missing or inaccessible")
        parameters["membership_id"] = membership.identifier
        if kind in {"remove_member", "publish_board"}:
            epoch = metadata.get("leadership_epoch")
            if not is_integer(epoch, 1):
                raise ClientError(
                    "forbidden", "The acting member has no room leadership"
                )
            parameters["expected_leadership_epoch"] = epoch
        if kind in TASKS or kind == "send_message":
            version = metadata.get("current_board_version")
            if not is_integer(version, 1):
                raise unavailable()
            parameters["expected_board_version"] = version
        return parameters

    def _block_if_denied(self, command: StoredCommand, failure: ClientError) -> None:
        if failure.code != "unavailable":
            self._store.block_command(
                command.operation_id, redact=failure.code in ACCESS_LOST
            )

    async def _dispatch(
        self, binding: ActiveBinding, command: StoredCommand, *, automatic: bool = False
    ) -> dict:
        self._check_binding(binding)
        if command.request is None or not self._store.mark_command_dispatch(
            command, automatic=automatic
        ):
            raise _blocked(command)
        try:
            result = await self._request(
                "POST", "/room-commands", binding.token, body=command.request
            )
        except (OSError, TimeoutError):
            raise ClientError(
                "command_outcome_unknown", "The room command outcome is unknown"
            ) from None
        except ClientError as exc:
            if exc.code == "unavailable":
                raise ClientError(
                    "command_outcome_unknown", "The room command outcome is unknown"
                ) from None
            if command.attempts:
                self._block_if_denied(command, exc)
                raise _blocked(command) from None
            self._store.reject_command(
                command.operation_id,
                exc.code,
                keep_draft=exc.code in REVIEWABLE_REJECTIONS,
            )
            raise
        receipt = self._validate_receipt(command, command.request, result)
        if receipt is None:
            raise ClientError(
                "command_outcome_unknown", "The room command outcome is unknown"
            )
        self._store.save_command_receipt(command.operation_id, receipt)
        if command.request["kind"] == "leave_room":
            self._store.invalidate_commands(
                binding.server_profile,
                binding.account_id,
                binding.member_id,
                room_id=command.room_id,
                membership_id=command.membership_id,
                keep_self_leave=True,
            )
        return receipt

    @staticmethod
    def _validate_receipt(
        command: StoredCommand, request: dict, result: dict
    ) -> dict | None:
        kind = request["kind"]
        parameters = request["parameters"]
        receipt = result.get("receipt")
        if (
            result.get("operation_id") != command.operation_id
            or result.get("server_generation") != command.generation
            or not isinstance(receipt, dict)
            or receipt.get("kind") != kind
            or not is_uuid(receipt.get("room_id"))
            or (command.room_id and receipt["room_id"] != command.room_id)
        ):
            return None
        validators = {
            "room_id": is_uuid,
            "membership_id": lambda value: is_string(value, 128),
            "member_id": is_uuid,
            "version": lambda value: is_integer(value, 1),
            "message_id": lambda value: is_integer(value, 1, (1 << 63) - 1),
            "task_id": is_uuid,
            "departure_event_id": lambda value: (
                value is None or is_integer(value, 1, (1 << 63) - 1)
            ),
        }
        if any(
            key not in validators or not validators[key](value)
            for key, value in receipt.items()
            if key != "kind"
        ):
            return None
        if kind == "send_message" and "message_id" not in receipt:
            return None
        if kind in TASKS and (
            "task_id" not in receipt
            or receipt["task_id"] != parameters.get("task_id", receipt["task_id"])
        ):
            return None
        if kind in {"create_room", "join_room"} and "membership_id" not in receipt:
            return None
        if (
            kind == "remove_member"
            and receipt.get("member_id") != parameters["target_member_id"]
        ):
            return None
        if kind in {"publish_board", "acknowledge_board"} and receipt.get(
            "version"
        ) != parameters["version"] + (kind == "publish_board"):
            return None
        return receipt

    @staticmethod
    def _leave_result(room_id: str, fields: dict, receipt: dict) -> dict:
        result: dict = {"room_id": room_id}
        if fields.get("notify"):
            result["departure_event_id"] = receipt.get("departure_event_id")
        return result

    def _leave_without_membership(
        self,
        scope: CommandScope,
        generation: str,
        fields: dict,
        retry_key: str | None,
        intent: str,
    ) -> dict:
        if retry_key is None:
            return self._leave_result(scope.room_id, fields, {})
        # Remember this explicit intent without referring it to a future membership.
        body = {
            "operation_id": str(uuid4()),
            "expected_member_id": scope.member_id,
            "server_generation": generation,
            "kind": "leave_room",
            "room_id": scope.room_id,
            "parameters": fields,
        }
        command = self._store.stage_command(
            scope, generation, None, retry_key, intent, body, blocked=True
        )
        if command is None:
            raise ClientError("command_pending", "The room has an unresolved command")
        raise _blocked(command)

    async def _hydrate(
        self,
        binding: ActiveBinding,
        generation: str,
        command: StoredCommand,
        kind: str,
        fields: dict,
        receipt: dict,
    ) -> dict:
        room_id = receipt["room_id"]
        path = f"/rooms/{room_id}"
        if kind == "leave_room":
            return self._leave_result(room_id, fields, receipt)
        if kind == "remove_member":
            if command.receipt is not None:
                membership = await self._membership(binding, room_id, generation)
                metadata = await self._room_metadata(binding, room_id, membership)
                if (
                    membership.identifier != command.membership_id
                    or metadata.get("can_remove_members") is not True
                ):
                    raise ClientError(
                        "forbidden", "The acting member has no room leadership"
                    )
            return {
                "room_id": room_id,
                "member_id": receipt["member_id"],
                "departure_event_id": receipt.get("departure_event_id"),
            }
        if kind == "acknowledge_board":
            return {"version": receipt["version"]}
        expected_membership: str | None = command.membership_id
        if kind in {"create_room", "join_room"}:
            expected_membership = receipt["membership_id"]
            result = await self._read(binding, path)
            keys = ["room_id", "name"]
            if kind == "create_room":
                keys.append("history_policy")
            if any(key not in result for key in keys):
                raise unavailable()
            result = {key: result[key] for key in keys}
        elif kind == "send_message":
            message_id = receipt["message_id"]
            history = await self._read(
                binding,
                f"{path}/messages",
                {
                    "membership_id": expected_membership,
                    "after": message_id - 1,
                    "limit": 1,
                },
            )
            rows = history.get("messages")
            if (
                not isinstance(rows, list)
                or len(rows) != 1
                or not isinstance(rows[0], dict)
                or rows[0].get("message_id") != message_id
                or rows[0].get("sender_member_id") != binding.member_id
                or rows[0].get("client_message_id") != fields["client_message_id"]
            ):
                raise unavailable()
            result = rows[0]
        elif kind in TASKS:
            task_id = receipt["task_id"]
            result = await self._read(binding, f"{path}/tasks/{task_id}")
            if result.get("task_id") != task_id:
                raise unavailable()
        else:
            result = await self._read(binding, f"{path}/board/view")
            if (
                result.get("version") != receipt["version"]
                or result.get("can_edit") is not True
            ):
                raise unavailable()
        membership = await self._membership(binding, room_id, generation)
        if (
            membership.identifier != expected_membership
            or result.get("room_id") != room_id
        ):
            raise ClientError("membership_changed", "The room membership changed")
        return result
