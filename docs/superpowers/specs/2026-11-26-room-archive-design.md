# Room archive design

The maintainer approved an in-place archive that closes a room when its creator marks the goal reached or after 24 hours of chat inactivity. Collaboration freezes each room and captures the roster in one write transaction. Authorized readers use the original message and task records under the existing access rules. The application retains room and DM history indefinitely and the existing hourly hp-laptop backups capture the complete database.

The [room archive contract](../../modules/room-archive.md) gives the caller and storage semantics. Keeping the archive in the canonical database lets a chat commit either before the freeze and appear in history, or after it and fail. A separate store would require coordinating a copy with the freeze and verifying two databases during recovery.
