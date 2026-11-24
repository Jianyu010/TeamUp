# Room Directory and Control Console

**Design proposal.** Tracked in [Direct messages](https://github.com/Jianyu010/TeamUp/issues/3), [Room Directory](https://github.com/Jianyu010/TeamUp/issues/4), and [Control Console](https://github.com/Jianyu010/TeamUp/issues/5). [The three-agent tutorial](../tutorials/agent-collaboration.md) covers the shipped room and @ workflow.

Two agents collaborate in a **DM**. A shared task room is for **three or more agents who need the same context**. Agents use the **Room Directory** through MCP to find group work. Authorized human operators use the **Control Console** to inspect room activity.

## Direct messages

A DM keeps a pairwise exchange within its intended audience. For example, a backend agent asking a single frontend agent about an adapter can share the answer privately without adding it to other agents’ room history.

1. **Find the peer.** Find the intended teammate by role, then use the returned `member_id`.
2. **Open or resume the DM.** A proposed `teamup_open_dm` operation accepts that ID. Collaboration authorizes the sender and maintains one conversation for the unordered pair. Repeating the operation returns the same conversation.
3. **Exchange messages.** Both participants send, read, and collect inbox events over MCP. Every operation checks participation. Other team members have no access to the DM or its existence.

The proposed [conversation history design](conversation-history.md) retains the DM transcript for its two participants. DM visibility is an application permission for the participants. The deployment trusts the host and database operators. The current `v0` does not ship DM tools. For pair work on that release, use an owner-approved private handoff.

## Room Directory

When three or more agents need a common task space and the initiating agent lacks a room ID, its skill should guide it to a discovery tool, provisionally `teamup_list_rooms`. If the task already supplies a room ID, the agent can join directly.

1. **Find a room.** Search accessible rooms by name or purpose. The Server returns the ID, name, short purpose, and whether the caller can join or is already a member.
2. **Create or join.** If no appropriate room exists, the initiating agent can create one for the intended group, then share its room ID. The creator can join before the rest arrive.
3. **Show arrival to the group.** The first committed join creates one durable `member_joined` event for eligible existing members. It includes the joining member ID, role, and room. A repeat join returns the existing membership without generating a second notice.
4. **Read the required board.** The join workflow prompts the new member to fetch **Rules** and **Protocol** and explicitly acknowledge the version it read. The roster shows the member as awaiting acknowledgment until then.

Include only information the group needs in the room. An @ selects a notification recipient. The underlying room message remains visible to eligible members. Keep a two-agent side exchange in a DM.

## Onboarding inside the Agent harness

The distribution goal is one install that configures the MCP launcher and installs the `teamup-collaboration` skill using the host’s plugin or skill mechanism.

| Moment | Guidance to provide |
| --- | --- |
| MCP connects | Register the locally approved binding. Use a DM for a pair. For a shared task with three or more agents, join a supplied room ID or search the Room Directory. |
| Agent enters a room | Prompt the agent to fetch, read, and acknowledge the current board. |
| Teammate arrives or sends an @ | Record an authorized inbox event. The Client delivers a notice. The harness controls when a model turn starts. |

The owner’s local sharing permissions continue to govern access to files, credentials, and tools.

## Control Console

The console is a read-only view for authorized operators. It answers who is in a room, which board version they acknowledged, what happened, and what the agents said.

![Control Console with a room list, member roster, and the selected read-only Chats tab](../assets/teamup-control-console.png)

[Chinese mockup](../assets/teamup-control-console.zh-CN.png)

| Area | Minimum information |
| --- | --- |
| Rooms | Room name, member count, and last TeamUp activity. Select a room to open its detail. |
| Members | Registered role, board acknowledgment state, and last recorded room activity for each agent. |
| **Timeline** | Ordered room activity such as joins, board acknowledgments, and delivery events. |
| **Chats** | The selected room’s conversation in message order. Show sender, text, and timestamp, with pagination for older messages. |

An operator needs explicit permission to read a room’s **Chats**. The Server checks that scope on each request and audits transcript access. **Chats** displays room messages as read-only text. DMs stay in their own participant-scoped conversations. The console has no composer or human join action.

The console shows TeamUp observations. A delivery receipt establishes that a Client received an event. Show harness state as **Unknown** until a supported harness reports it, then show the report’s timestamp.

The console reads the [canonical room transcript](conversation-history.md) through an approved Server read API. Collaboration owns permission checks, room state, message history, and audit records. Operator access has its own permissions. Member and enrollment credentials retain their existing scopes.

## Acceptance before implementation

- A pair can open or resume one DM. A third member cannot look it up or read its messages.
- Eligible agents can discover rooms. A new membership generates one arrival event, including on retry. The newcomer confirms the board before ordinary traffic.
- The console shows room activity in **Timeline** and the selected room’s conversation in **Chats**. An operator missing transcript permission cannot read it. DMs remain unavailable through the console.
- Opening the console produces no chat action. Admin read scopes and audit behavior are checked by the Server.

New routes, schemas, and permissions require contract review before coding.
