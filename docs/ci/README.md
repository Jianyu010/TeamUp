# CI, automated merges, and releases

TeamUp uses the merge gate to merge changes into `main` automatically. Only the repository owner (`Jianyu010`) can merge manually. The [shared CI contract](contract.md) defines the fields, paths, and commands the suites use. The [labeling standard](labeling.md) governs issue and PR labels.

## CI layers

TeamUp follows DigitalMate's naming convention. The job display name is `<layer> · <name>`. The runner label is the lowercase, hyphenated job name, and the registered runner name appends a two-digit suffix, as in `linux-ut-01`.

| Layer | Jobs or checks | When it runs | What happens on failure |
| --- | --- | --- | --- |
| 1 · Routing | The merge gate checks for a missing linked issue, changes to protected paths, deleted tests, new skip or xfail markers, and changes spanning more than 300 files | Every gate evaluation | Link an issue. When `require_ready` is on, the other cases also wait for the owner to review and apply `ready` |
| 2 · Static checks | `2 · Lint` (ruff) and `2 · Architecture` (dependency direction) | Every push, including draft PRs | The gate blocks the merge |
| 3 · Tests and review | `3 · Linux UT` (component and CI unit tests), `3 · Linux ST` (cross-process and MCP integration), and `3 · AI Review` | After the PR is marked ready for review. The gate dispatches AI Review once CI passes | The gate blocks the merge. `3 · Review Findings` fails when a P0 or P1 finding remains unresolved or a P2 finding goes unanswered |
| 4 · End-to-end | `4 · Nightly` (model-free scenarios and performance tests) and `4 · Weekly` (real agents) | On a schedule for `main` | A nightly failure freezes `main`. A weekly failure opens a `weekly-failure` issue |
| 5 · Merge | `5 · Merge Gate` (the `merge-gate` status) and `5 · Merge` | When checks finish, labels change, or the 15-minute scan runs | The PR stays in the queue |

Draft PRs run only layer 2, leaving the unit test, system test, and review slots available. Marking a PR ready for review reruns `CI` and starts layer 3.

### Additional checks in layers 1–3

All checks below are enabled and follow this contract. Checks marked required appear in `.github/ci/policy.json`. The job names, runner labels, commands, and failure labels in this table are binding.

| Job | Layer | Runner (label = hyphenated name) | Command or decision rule | Required | Failure label | Status |
| --- | --- | --- | --- | --- | --- | --- |
| `1 · PR Routing` | 1 | Commit status written by the gate (`merge-gate-01`) | Reports whether the PR can proceed automatically or needs owner review | No, reports only | – | Enabled |
| `1 · Protected Changes` | 1 | Same as above | Fails for changes to protected paths, deleted tests, or new skip or xfail markers. The owner can clear it with `ready`. When `require_ready` is off, it only reports: it stays green with a note | No, reports only | – | Enabled |
| `1 · Automation Limits` | 1 | Same as above | Fails if the PR has an untrusted author, has exhausted its review reruns, or (when `require_ready` is on) changes more than 300 files | No, reports only | – | Enabled |
| `1 · Linked Issue` | 1 | Same as above | Fails if the title or body lacks a reference to an issue in this repository (`#N` or `Jianyu010/TeamUp#N`). References to PRs do not count. The gate blocks the merge unless the owner applies `ready` | No, enforced by the gate | – | Enabled |
| `3 · Review Findings` | 3 | Same as above | Fails if any P0 or P1 finding remains unresolved or any P2 finding goes unanswered, as described under AI review findings below. Stays pending until the current head has been reviewed | No, enforced by the gate | – | Enabled |
| `2 · Type Check` | 2 | `type-check-01` | `uv run --no-sync mypy` uses `[tool.mypy]` in the root `pyproject.toml` and covers `server/src`, `client/shared/src`, and `plugin/mcp/src` | Yes | `gate/type-check-failed` | Enabled |
| `2 · Secrets` | 2 | `secrets-01` | `gitleaks dir . --config .gitleaks.toml --redact --exit-code 1` | Yes | `gate/secrets-failed` | Enabled |
| `2 · Workflow Lint` | 2 | `workflow-lint-01` | `actionlint -config-file .github/actionlint.yaml` (lists self-hosted runner labels) and `zizmor --offline .github/workflows` (uses `.github/zizmor.yml`) | Yes | `gate/workflow-lint-failed` | Enabled |
| `2 · Dependencies` | 2 | `dependencies-01` | `uv lock --check` verifies that `uv.lock` agrees with each `pyproject.toml`, after a temporary copy of the lockfile is rewritten to use the Tsinghua mirror. `pip-audit` reports vulnerabilities in the job summary without failing the job | Yes, lockfile check only | `gate/dependencies-failed` | Enabled |
| `3 · Contract` | 3 | `contract-01` | `uv run --no-sync pytest -q tests/contract` checks HTTP APIs, SSE events, and MCP tool schemas against the snapshots in `tests/contract/golden/` | Yes | `gate/contract-failed` | Enabled |
| `3 · Install` | 3 | `install-01`–`03` | `uv run --no-sync pytest -q tests/install` builds wheels for all three packages, installs them in a fresh virtual environment, and checks their CLI entry points | Yes | `gate/install-failed` | Enabled |
| Coverage minimum for `3 · Linux UT` | 3 | `linux-ut-01`–`03` | `pytest --cov` enforces `fail_under` in `[tool.coverage.report]` in the root `pyproject.toml` (currently 48%, with measured coverage around 50%) | Yes, as part of `3 · Linux UT` | `gate/linux-ut-failed` | Enabled |
| `3 · Linux ST · 跨组件` | 3 | `linux-st-cross-01`–`03` | `uv run --no-sync pytest -q tests/system` starts the Server, Client daemon, and MCP together, then exercises registration, room creation, announcements, messaging, and notifications | Yes | `gate/linux-st-cross-failed` | Enabled |

The four layer 2 checks above also run on drafts. Layer 3 starts only once a PR is marked ready for review. The gate writes the three layer 1 statuses without running PR code. Branch protection uses only `merge-gate` for the merge decision.

Protected paths and required checks are listed in [`.github/ci/policy.json`](../../.github/ci/policy.json), which is itself protected.

`3 · Linux UT` runs the Python tests in `tests/server/`, `tests/client/shared/`, `tests/client/macos/`, and `tests/scripts/ci/`. `3 · Linux ST` runs `tests/integration/` and `tests/plugin/mcp/`. The macOS console and shared UI Node tests run via `npm --prefix client/macos/desktop test`.

## Merge rules

- Branch protection on `main` requires a PR and a passing `merge-gate` status. Administrators are exempt, so the owner can merge manually.
- `merge-gate` passes only when all of the following are true.
  - The PR is ready for review.
  - The PR has no `pr/hold` label.
  - All required checks pass.
  - The current head passes `3 · AI Review` and its findings have been addressed as described below, or a maintainer has applied `ready`.
  - The PR links at least one issue. Editing the title or body triggers another evaluation. Removing the reference puts the gate back in pending.
  - No layer 1 routing check blocks the PR.
- The merge worker runs on `merge-01` on hp-laptop using the `TEAMUP_MERGE_TOKEN` repository secret. It squash-merges one PR at a time.
- Only users with the admin or maintain role can add `ready`. Pushing new commits automatically removes the label. See the `require_ready` switch below: when it is off, protected changes, oversized PRs, and freezes no longer need `ready`. PRs from untrusted authors, and P0 or P1 findings whose threads were resolved without a fix, still need a maintainer's `ready`.
- `pr/hold` blocks the PR until the label is removed.

### Owner review switch (`require_ready`)

`require_ready` in `.github/ci/policy.json` decides which cases need the owner's `ready`. It is currently `false`: the owner turned it off because waiting for `ready` slowed merges down. Any value other than an explicit `false`, including a missing key, counts as on.

| Case | `require_ready: true` | `require_ready: false` (current) |
| --- | --- | --- |
| Changes to protected paths, deleted tests, or new skip or xfail markers | Wait for the owner's `ready` | The PR takes the automatic route. `1 · Protected Changes` stays green with a note |
| More than 300 changed files | Wait for the owner's `ready`. `1 · Automation Limits` fails | Not blocked and not reported. A PR with 3,000 or more files (GitHub's listing cap, so the list may be incomplete) always needs AI review, whatever the switch says |
| A freeze on `main` or nightly | Only PRs with `ready` or a freeze reference merge | Merges are not blocked. The freeze issue still opens and closes as a signal, so a PR that would close it (`Closes`, `Fixes`, `Resolves`) still waits. If the freeze issues cannot be read, merging goes on with a warning |

These rules hold whatever the switch says: all required checks pass, `3 · AI Review` passes and its findings are addressed as described below, the PR links an issue, `pr/hold` blocks, drafts do not merge, and PRs from untrusted authors wait for a maintainer. `ready` still works as an override (for example, for unfixed P0 or P1 findings), but it is no longer required. The switch is per repository rather than an exception for the owner, because PRs are normally authored by the owner's account, which agents also use, so exempting the owner would turn it off for every PR anyway.

### AI review findings

The reviewer assigns a severity to each finding. `critical` maps to P0, `high` to P1, and `medium` to P2. The gate reads the review findings ledger and publishes the `3 · Review Findings` commit status.

- **Fix P0 and P1 findings.** A finding counts as addressed once the reviewer confirms the fix (`fixed`) or accepts the author's explanation (`answered`) in a later round. Author replies and resolved threads alone do not clear these findings. After the fix is pushed, the reviewer checks it in the next round.
- **Respond to P2 findings.** The PR author or owner can reply in the finding's thread with the fix or the reason for keeping the current behavior, mark the thread resolved, or post a reply starting with `F<n>: ...` in the PR conversation. Findings the reviewer marks as fixed also count as addressed.
- While any required fixes or responses are outstanding, `3 · Review Findings` fails and `merge-gate` stays pending. The gate reports the outstanding findings.
- The reviewer skips P0 and P1 findings whose threads were marked resolved before they were fixed. Only the owner can clear those findings by reviewing the PR and applying `ready`.

### Merge freezes

A failed `CI` push run on `main` or a failed nightly run opens an issue labeled `ci/freeze`.

When `require_ready` is on, during a freeze a PR can merge if it has `ready` or if it fixes an open freeze issue and cites that issue in its title or body using `Refs #N`, `Ref #N`, or `Part of #N`. A fix submitted through the second route still needs a passing gate, and it can proceed without waiting for the owner. Closing keywords such as `Closes`, `Fixes`, and `Resolves` make the PR ineligible for that route because GitHub would close the freeze issue as soon as the PR merged. The merge worker rereads the PR before merging and stops if the reference has been removed or changed to a closing keyword. The freeze issue closes automatically once `main` is green again and nightly passes. A weekly failure only opens a `weekly-failure` issue.

## Runners

All jobs use self-hosted runners on the owner's hp-laptop. Each runner has its own home directory, runs in a bwrap sandbox, and is supervised by `~/opencode-review/runner/runner-ctl`. Register runners using [`scripts/ci/hp-laptop/register-runners.sh`](../../scripts/ci/hp-laptop/register-runners.sh).

| Runner | Label | Code it runs | Jobs |
| --- | --- | --- | --- |
| `lint-01` | `lint` | PR code | `2 · Lint` |
| `architecture-01` | `architecture` | PR code | `2 · Architecture` |
| `type-check-01` | `type-check` | PR code | `2 · Type Check` |
| `secrets-01` | `secrets` | PR code | `2 · Secrets` |
| `workflow-lint-01` | `workflow-lint` | PR code | `2 · Workflow Lint` |
| `dependencies-01` | `dependencies` | PR code | `2 · Dependencies` |
| `linux-ut-01`–`03` | `linux-ut` | PR code | `3 · Linux UT` |
| `linux-st-01`–`03` | `linux-st` | PR code | `3 · Linux ST` |
| `linux-st-cross-01`–`03` | `linux-st-cross` | PR code | `3 · Linux ST · 跨组件` |
| `contract-01` | `contract` | PR code | `3 · Contract` |
| `install-01`–`03` | `install` | PR code | `3 · Install` |
| `ai-review-01`–`03` | `ai-review` | Review tooling from `main` | `3 · AI Review` (also subject to the shared review concurrency limit) |
| `e2e-01` | `e2e` | `main` | `4 · Nightly` and `4 · Weekly` (run one at a time to avoid skewing performance measurements) |
| `merge-gate-01` | `merge-gate` | Scripts from `main` only | `5 · Merge Gate`, revocation, `main` health checks, and post-review wakeups |
| `merge-01` | `merge` | Scripts from `main` only | `5 · Merge` |
| `gate-tick-01` | `gate-tick` | `main` | Scheduler tick every 15 minutes |
| `utility-01` | `utility` | Scripts from `main` only | Suite reporting, releases, dashboards, and issue and PR labeling |

The long-running layer 3 jobs have three concurrent slots each. [`scripts/ci/hp-laptop/install-tools.sh`](../../scripts/ci/hp-laptop/install-tools.sh) installs gitleaks, actionlint, shellcheck, zizmor, and pip-audit in the home directories of the relevant layer 2 runners. It checks the SHA-256 hashes of the binaries. Runner directories on hp-laptop use the `teamup-` prefix (for example, `~/gh-runner/teamup-linux-ut-01`) to distinguish them from runners for other repositories on the same machine. The GitHub registration names omit the prefix.

- Runners that execute PR code receive no secrets.
- Jobs triggered by `pull_request_target` fetch scripts from `main` via the contents API without checking out PR code.
- The runners fetch source trees as API tarballs because hp-laptop frequently cannot reach GitHub's Git service.
- PyPI requests go through the Tsinghua mirror. The runners rewrite the URLs in a temporary `uv.lock` before running `uv sync --frozen`.
- Model requests go through a local proxy at `127.0.0.1:18081`. The gateway secret stays out of runner home directories.

## Scheduling

GitHub cron runs often start 3–5 hours late. `gate-tick.yml` runs every 15 minutes to advance the merge queue and dispatch nightly, weekly, and dashboard jobs when they are due under the `cadence` settings in `policy.json`.

| Job | Schedule (Asia/Shanghai) |
| --- | --- |
| nightly | Every day at 02:00 |
| weekly | Every Sunday at 04:00 |
| Dashboard statistics | Every day at 09:30 |

You can also dispatch any of the three jobs from the Actions page.

## Versions and releases

Release tags use the format `vX.Y.Z`. The baseline tag is `v0.1.0`.

- **Development builds.** Builds on `main` use `X.Y.(Z+1).devN`, where N counts first-parent commits on `main` since the latest tag. `scripts/ci/version.py` computes the version and writes it to the results file's `version` field.
- **Patch releases.** Tag the next patch release, `X.Y.(Z+1)`, when nightly passes and there are new commits since the last tag (N > 0).
- **Minor releases.** Tag the next minor release, `X.(Y+1).0`, when weekly passes and a PR has been merged since `vX.Y.0`.
- **Release notes.** `scripts/ci/release.py` generates the notes deterministically without calling a model. They include the merged PRs by category, the closed issues, the contributors, and a table of evidence for the suite with scenario counts, pass counts, and run links.
- The release script requires a target commit newer than the latest tag.

## Dashboards

Results and statistics are published to the `ci-data` branch. GitHub Pages serves the site from the root of that branch. See the [dashboard README](../../dashboards/README.md) for a guide to the pages.

| Page | Data |
| --- | --- |
| Overview | Latest nightly and weekly status, version, and repository summary |
| CI | Daily pass rates for CI and AI Review on PRs and `main`, nightly and weekly pass rates, merge throughput, freeze status, and scenario stability |
| Repository | Stars, forks, issues and PRs, downloads, and time since the last release |
| Performance | Nightly performance, including DM latency, five-agent rooms at three load levels, and performance degradation as history grows |
| Agents | Weekly scenarios and metrics from real agents |

## Labels

`issues.yml` processes new issues in this order.

1. Apply `agent/human-only` and `status/review-needed` to issues opened by people and assign them to the owner.
2. Use the model on hp-laptop to apply `type`, `source`, `priority`, `area`, and `size` labels according to the [labeling standard](labeling.md).
3. Check the required label categories and apply `status/triage` if any are missing.

The `pr-labels` job in `gate-queue.yml` maintains `gate/*-failed` and `pr/no-issue` on PRs and mirrors `type/*` from linked issues. These are informational labels. The gate checks for linked issues independently via `1 · Linked Issue`, using the same rule as `pr/no-issue`. Release notes group PRs by `type/*`.

## Maintainer operations

- **Approve a protected change.** Review the diff and apply `ready`. This is needed only when `require_ready` is on.
- **Hold a PR.** Apply `pr/hold`.
- **Reevaluate the gate.** Run `Gate queue` from Actions. You can optionally provide a PR number.
- **Rotate the merge token.** Replace the `TEAMUP_MERGE_TOKEN` secret with a fine-grained token scoped to this repository. Grant these permissions.
  - Read and write for Contents, Pull requests, Issues, and Workflows.
  - Read-only for Metadata.
  - Optional read-only access to Administration for traffic statistics.
- **Add a runner.** Follow the comments in the registration script and pass the registration token through stdin to keep it out of command-line arguments.
