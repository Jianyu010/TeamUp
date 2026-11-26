# Labeling standard

Labels drive TeamUp's automation. Everyone who applies labels, including agents, follows this standard. It uses Linkie's scheme from the PPMA repository, with `area/*` values that match TeamUp's components. Create labels only from the inventory below. If you need a new `area/*` or `gate/*` value, add it to the inventory in the same PR.

## Conventions

- Use lowercase `category/value` labels with hyphens between words.
- Each category has its own color, listed in the inventory.
- Every issue needs at least one `type/*`, one `source/*`, and one `priority/*` label. If any required category is missing, automation applies `status/triage` and posts a comment identifying what is missing. Wait until `status/triage` is cleared before starting work.
- The `scripts/ci/labels.py lint` command checks the number of labels in each category.

| Category | Number of labels |
| --- | --- |
| `type/*`, `source/*` | One or more |
| `priority/*` | Exactly one |
| `size/*`, `agent/*` | Zero or one |
| `area/*`, `status/*`, `gate/*` | Any number |

## Issue labels

### `type/*` for the kind of work (one or more)

Apply multiple labels when the work spans categories, such as `type/feature` and `type/docs`.

| Label | Meaning |
| --- | --- |
| `type/bug` | Behavior differs from expectations or documentation |
| `type/perf` | Improved latency, memory use, throughput, or cost while preserving behavior |
| `type/feature` | New capability |
| `type/enhancement` | A non-performance improvement to an existing capability |
| `type/refactor` | Internal restructuring that preserves behavior |
| `type/test` | Test coverage, flaky tests, or test infrastructure |
| `type/ci` | Build, CI, merge gates, tooling, or the development environment |
| `type/docs` | Documentation-only change |
| `type/security` | Vulnerability or hardening |
| `type/chore` | Dependency updates or routine maintenance |

### `source/*` for where the issue surfaced (one or more)

If the same issue surfaces independently in several places, apply a label for every source. Reassess the priority when multiple sources point to a wider impact.

| Label | Meaning |
| --- | --- |
| `source/incidental` | Found while working on something else |
| `source/planned` | Roadmap item or planned work |
| `source/review` | Raised during a PR review and outside that PR's scope |
| `source/ci` | CI failure on `main` |
| `source/user` | Reported by a person, including maintainers and team members |
| `source/nightly` | Found by nightly |
| `source/weekly` | Found by weekly |
| `source/routine` | Found by other scheduled checks, such as dependency checks or security scans |

### `priority/*` for urgency (exactly one)

| Label | Meaning | Action |
| --- | --- | --- |
| `priority/critical` | Broken `main`, data loss, a security vulnerability, or work at a standstill | Drop current work and notify a person |
| `priority/high` | High impact | Handle next |
| `priority/medium` | Normal backlog item | Use when unsure |
| `priority/low` | Nice to have | Handle when higher-priority work is clear |

### `blocker` for release requirements (any priority)

Close the issue before the next release or milestone. Priority indicates how soon to work on the issue. The `blocker` label indicates whether the release can ship while the issue is open.

### Optional categories

| Label | Meaning |
| --- | --- |
| `area/<component>` | Component or subsystem, using the values below |
| `size/s` | About an hour or less |
| `size/m` | About half a day |
| `size/l` | Needs a design or plan before implementation |
| `agent/ok` | A maintainer has authorized an agent to work unattended |
| `agent/human-only` | A person must make the decision. Agents must leave implementation to people |

Automatic labeling can suggest only `type`, `source`, `priority`, `area`, and `size`. Only a maintainer can grant or revoke permission for unattended agent work via `agent/ok`. The `agent/human-only` label overrides `agent/ok`.

### `status/*` for issue lifecycle

| Label | Applied by | Meaning |
| --- | --- | --- |
| `status/triage` | Automation | A required label category is missing |
| `status/claimed` | The person or agent starting work | Prevents duplicate work on the same issue |
| `status/blocked` | A person or agent | Waiting on another issue or PR. Link to it in a comment |
| `status/needs-info` | A person or agent | Needs clarification before work can continue |
| `status/review-needed` | Automation | Issue opened by a person and awaiting maintainer review. Always paired with `agent/human-only` |

Use GitHub's close reasons for duplicates and issues that will not be fixed.

**Issues opened by people.** New issues whose authors are not bots get both `agent/human-only` and `status/review-needed`. If the issue is unassigned, automation assigns it to the repository owner and mentions the owner in a comment. Reruns are idempotent. Labels removed by a maintainer are restored only on a manual rerun. Skip this step for bot-created issues (such as CI freezes, weekly failures, and dependency updates) and issues already labeled `agent/ok`.

## Automatic labeling

When a new issue opens, the model on hp-laptop chooses `type`, `source`, `priority`, `area`, and `size` labels from the inventory. The `scripts/ci/autolabel.py` script validates the selections.

- Keep only listed labels from those five categories.
- Exclude `agent/*`, `blocker`, and `priority/critical`.
- Default to `priority/medium` and `source/user` if the model leaves out a priority or source.
- Skip issues a person has already labeled with a `type`, `source`, `priority`, `agent`, or `blocker` label.

The issue body is untrusted and goes only to the model. It never enters a shell. The `AUTOLABEL_MODE` repository variable accepts these values.

- `apply` applies the labels directly. This is the default.
- `suggest` posts the suggested labels in a comment.
- `off` disables automatic labeling.

Set `AUTOLABEL_MODEL` to override the default model.

## PR labels

PR labels describe PR status. The `type/*` labels come from associated issues.

| Label | Applied by | Meaning |
| --- | --- | --- |
| `ready` | Maintainer only | A maintainer reviewed the PR and approved bypassing a layer 1 block or AI review, or merging during a freeze. Removed automatically on a new push |
| `pr/hold` | Person only | Block merging even when the gate passes |
| `pr/no-issue` | Automation | The title and body contain no references to an issue in this repository (`#N`, `Closes #N`, `Refs #N`, and similar forms). References to PRs do not count. The `1 · Linked Issue` gate check also blocks merging. The owner can allow the merge with `ready` |
| `gate/<check>-failed` | Automation | The check failed. The label clears when the check passes again |
| `type/*` (mirrored) | Automation | Copied from linked issues to group PRs in release notes |

Repository-wide status labels

| Label | Applied by | Meaning |
| --- | --- | --- |
| `ci/freeze` | Automation | A failure on `main` or in nightly restricts merges to PRs with `ready` |
| `weekly-failure` | Automation | Weekly end-to-end tests with real agents failed |
| `opencode-review` | Maintainer | Request one OpenCode review of a PR |

## Rules for agents

1. Always set `type/*`, `source/*`, and `priority/*` when filing an issue. Include every applicable `type/*` label. If you are unsure about urgency, use `priority/medium` and explain why in the issue body. Add `area/*` and `size/*` when you can determine them.
2. File a separate issue with `source/incidental` for problems you discover while working on something else. Keep them out of the current PR.
   - Search for an existing issue before filing a new one.
   - If there is already an issue, add your `source/*` and any missing `type/*` labels. Comment on where you encountered the problem and why it matters to your current work.
   - If the new evidence shows greater impact, suggest a priority change in a comment. The highest priority an agent can raise an issue to on its own is `priority/high`.
3. Reread the issue before starting work. If someone else is assigned, leave them a comment and wait. Otherwise, assign the issue to the developer you represent, apply `status/claimed`, and reread the issue to verify that the developer is the sole assignee. Remove the assignment and label if you give up the issue.
4. Do not add or remove `pr/hold`, `ready`, `agent/ok`, `agent/human-only`, or `blocker` at your own discretion. File an issue with `priority/critical` only when you have concrete evidence, and notify a person.
5. Leave `pr/*` and `gate/*` labels to automation.
6. Use only the labels in this standard.

## Component labels

The `area/*` labels follow the component diagram in the [architecture docs](../architecture/README.md) and the repository layout. Sensitive areas always need human review and are ineligible for unattended agent work.

| Label | Scope | Sensitive |
| --- | --- | --- |
| `area/collaboration` | `server/src/teamup_server/collaboration.py` handles members, rooms, DMs, announcements, messages, inboxes, and deactivation, and is the sole owner of the collaboration SQLite database | Yes |
| `area/gateway` | `server/src/teamup_server/{gateway,main}.py` covers the HTTPS API, SSE, protocol encoding and decoding, and Server configuration | |
| `area/presence` | Presence covers heartbeats, health queries, and harness observations | |
| `area/accounts` | Login, accounts, and credentials in `account_cli.py` and the Client's `_login.py` | Yes |
| `area/client` | `client/` covers the session coordinator, daemon, IPC, local configuration, and connectivity to the Server | |
| `area/harness` | Harness Driver and wakeups, including desktop notifications and wakeups for Claude Code and Codex | |
| `area/mcp` | MCP tool adapter in `plugin/mcp` for the `teamup_*` tools | |
| `area/plugin` | Packaging in `plugin/` and skills in `plugin/skills` | |
| `area/contracts` | Public contracts in `docs/modules/`, `docs/protocols/`, `docs/architecture/`, and `docs/specs/` | Yes |
| `area/ci` | `.github/`, `scripts/ci/`, `tests/scenarios/`, and `dashboards/` | Yes |

The `gate/*` labels correspond to layer 2 and 3 checks. Each failure has one label based on the hyphenated job name.

| Label | Failed check |
| --- | --- |
| `gate/lint-failed` | `2 · Lint` (ruff) |
| `gate/architecture-failed` | `2 · Architecture` (dependency direction) |
| `gate/linux-ut-failed` | `3 · Linux UT` |
| `gate/linux-st-failed` | `3 · Linux ST` |
| `gate/ai-review-failed` | `3 · AI Review` run failed |
| `gate/type-check-failed` | `2 · Type Check` (mypy) |
| `gate/secrets-failed` | `2 · Secrets` (gitleaks) |
| `gate/workflow-lint-failed` | `2 · Workflow Lint` (actionlint and zizmor) |
| `gate/dependencies-failed` | `2 · Dependencies` (lockfile consistency) |
| `gate/contract-failed` | `3 · Contract` |
| `gate/install-failed` | `3 · Install` |
| `gate/linux-st-cross-failed` | `3 · Linux ST · 跨组件` |

## Label inventory

Use the label names, colors, and descriptions in this table. Run `python3 -m scripts.ci.labels sync` to create or update labels. The command leaves labels outside the inventory in place.

| Label | Color | Description |
| --- | --- | --- |
| type/bug | 1D76DB | Behavior differs from expectations |
| type/perf | 1D76DB | Faster or more efficient without changing behavior |
| type/feature | 1D76DB | New capability |
| type/enhancement | 1D76DB | Improve an existing capability |
| type/refactor | 1D76DB | Restructure code while preserving behavior |
| type/test | 1D76DB | Tests and test infrastructure |
| type/ci | 1D76DB | Build, CI, merge gates, and tools |
| type/docs | 1D76DB | Documentation-only change |
| type/security | 1D76DB | Vulnerability or hardening |
| type/chore | 1D76DB | Dependencies and routine maintenance |
| source/incidental | 5319E7 | Found while working on something else |
| source/planned | 5319E7 | Roadmap item or planned work |
| source/review | 5319E7 | Raised during a PR review and outside that PR's scope |
| source/ci | 5319E7 | CI failure on main |
| source/user | 5319E7 | Reported by a person |
| source/nightly | 5319E7 | Found by nightly |
| source/weekly | 5319E7 | Found by weekly |
| source/routine | 5319E7 | Found by another scheduled check |
| priority/critical | B60205 | Drop current work and notify a person |
| priority/high | D93F0B | Handle next |
| priority/medium | FBCA04 | Normal backlog item |
| priority/low | 0E8A16 | Nice to have |
| blocker | 000000 | Must be closed before the next release |
| size/s | BFD4F2 | About an hour or less |
| size/m | BFD4F2 | About half a day |
| size/l | BFD4F2 | Needs a design first |
| agent/ok | 0E8A16 | Agent can work unattended |
| agent/human-only | D876E3 | Needs a human decision |
| status/triage | EDEDED | Missing required labels |
| status/claimed | EDEDED | Someone is already working on this |
| status/blocked | EDEDED | Waiting on another issue or PR |
| status/needs-info | EDEDED | Needs clarification |
| status/review-needed | EDEDED | Issue opened by a person and awaiting maintainer review |
| ready | 0E8A16 | Maintainer approval for a protected change, AI review bypass, or merge during a freeze |
| pr/hold | B60205 | Block merging |
| pr/no-issue | FBCA04 | No linked issue |
| ci/freeze | D93F0B | main or nightly failed, merge only PRs labeled ready |
| weekly-failure | FBCA04 | Weekly end-to-end tests with real agents failed |
| opencode-review | 5319E7 | Request an OpenCode review |
| area/collaboration | C5DEF5 | Members, rooms, DMs, announcements, messages, and inboxes |
| area/gateway | C5DEF5 | HTTPS API, SSE, and protocol |
| area/presence | C5DEF5 | Heartbeats and health |
| area/accounts | C5DEF5 | Login, accounts, and credentials |
| area/client | C5DEF5 | Client session coordinator and daemon |
| area/harness | C5DEF5 | Harness Driver, desktop notifications, and wakeups |
| area/mcp | C5DEF5 | MCP tool adapter |
| area/plugin | C5DEF5 | Plugin packaging and skills |
| area/contracts | C5DEF5 | Public contracts, protocols, and architecture docs |
| area/ci | C5DEF5 | CI, scenario suites, and dashboards |
| gate/lint-failed | E99695 | Lint check failed |
| gate/architecture-failed | E99695 | Dependency direction check failed |
| gate/linux-ut-failed | E99695 | Linux unit tests failed |
| gate/linux-st-failed | E99695 | Linux system tests failed |
| gate/ai-review-failed | E99695 | AI review failed |
| gate/type-check-failed | E99695 | Type check failed |
| gate/secrets-failed | E99695 | Secret scan found a problem |
| gate/workflow-lint-failed | E99695 | Workflow check failed |
| gate/dependencies-failed | E99695 | Lockfile disagrees with dependency declarations |
| gate/contract-failed | E99695 | Contract tests failed |
| gate/install-failed | E99695 | Install smoke tests failed |
| gate/linux-st-cross-failed | E99695 | Cross-component system tests failed |
