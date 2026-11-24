#!/usr/bin/env bash
# Register TeamUp's self-hosted runners on hp-laptop (docs/ci/README.md, "Runner").
#
# Copy it to the box, then pipe a repository registration token in (never on a command line):
#   scp scripts/ci/hp-laptop/register-runners.sh jump@hp-laptop:teamup-register-runners.sh
#   gh api -X POST repos/Jianyu010/TeamUp/actions/runners/registration-token --jq .token \
#     | ssh jump@hp-laptop 'bash ~/teamup-register-runners.sh'
#
# Each runner is a sandboxed instance under ~/gh-runner managed by ~/opencode-review/runner
# (bwrap, per-instance home, cron-supervised by `runner-ctl ensure`). Already registered
# instances are left alone. Roles:
#   teamup-gate-1/2  label teamup-gate      PR code (layers 2-3); uv + Python 3.12 seeded
#   teamup-merge-1   label teamup-merge     trusted: gate, queue, merger, reports, releases
#   teamup-review-1  label opencode-review  the OpenCode reviewer (shared ~/opencode-review)
#   teamup-tick      label teamup-tick      the 15-minute tick (sleeps between dispatches)
#   teamup-suite-1   label teamup-suite     nightly and weekly suites (main's code; LLM relay)
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin
REPO=${TEAMUP_REPO:-Jianyu010/TeamUp}
BASE=/home/jump/gh-runner
APP=/home/jump/opencode-review
SEED=$BASE/state/ppma-gate-1/home # a home with uv, Python 3.12 and the mirror's uv.toml
PKG=$(ls "$BASE"/.cache/actions-runner-linux-x64-*.tar.gz | sort -V | tail -1)

IFS= read -r token
[[ $token =~ ^[A-Z0-9]{20,64}$ ]] || { echo "stdin is not a runner registration token" >&2; exit 2; }

ROLES=(
  "teamup-gate-1 teamup-gate"
  "teamup-gate-2 teamup-gate"
  "teamup-merge-1 teamup-merge"
  "teamup-review-1 opencode-review"
  "teamup-tick teamup-tick"
  "teamup-suite-1 teamup-suite"
)

for row in "${ROLES[@]}"; do
  read -r inst label <<<"$row"
  dir=$BASE/$inst
  home=$BASE/state/$inst/home
  if [[ -f $dir/.runner ]]; then
    echo "$inst: already registered"
    continue
  fi
  mkdir -p "$dir" "$home"
  [[ -f $dir/run.sh ]] || tar -xzf "$PKG" -C "$dir"
  case $label in
    teamup-gate | teamup-suite)
      cp -a "$SEED/.local" "$home/"
      mkdir -p "$home/.config" && cp -a "$SEED/.config/uv" "$home/.config/"
      {
        echo "LANG=C.UTF-8"
        echo "PATH=/home/jump/.local/bin:/usr/local/bin:/usr/bin:/bin"
        echo "UV_PYTHON_DOWNLOADS=never"
      } >"$dir/.env"
      if [[ $label == teamup-suite ]]; then
        {
          echo "TEAMUP_OPENCODE=/home/jump/opencode-review/opencode/node_modules/.bin/opencode"
          echo "TEAMUP_LLM_BASE_URL=http://127.0.0.1:18081/v1"
        } >>"$dir/.env"
      fi
      ;;
    opencode-review)
      {
        echo "LANG=C.UTF-8"
        echo "OPENCODE_REVIEW_HOME=$APP"
        echo "REVIEW_LLM_BASE_URL=http://127.0.0.1:18081/v1"
      } >"$dir/.env"
      ;;
    *)
      echo "LANG=C.UTF-8" >"$dir/.env"
      ;;
  esac
  "$APP/runner/runner-ctl" stop "$inst" >/dev/null 2>&1 || true
  "$APP/runner/runner-sandbox" "$inst" ./config.sh --unattended --replace \
    --url "https://github.com/$REPO" --token "$token" \
    --name "hp-laptop-$inst" --labels "$label" --work _work >/dev/null
  "$APP/runner/runner-ctl" start "$inst"
  echo "$inst: registered with label $label"
done
