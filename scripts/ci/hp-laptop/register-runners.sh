#!/usr/bin/env bash
# Register TeamUp's self-hosted runners on hp-laptop (docs/ci/README.md, "运行器").
#
# Names follow DigitalMate's scheme: the runner label is the job name in kebab case (`linux-ut`
# for `3 · Linux UT`) and each runner is the label plus a two-digit index (`linux-ut-01`). The
# box-local instance directory adds a `teamup-` prefix, since other repositories' runners share
# ~/gh-runner.
#
# Copy it to the box, then pipe a repository registration token in (never on a command line):
#   scp scripts/ci/hp-laptop/register-runners.sh jump@hp-laptop:teamup-register-runners.sh
#   gh api -X POST repos/Jianyu010/TeamUp/actions/runners/registration-token --jq .token \
#     | ssh jump@hp-laptop 'bash ~/teamup-register-runners.sh'
#
# Each runner is a sandboxed instance managed by ~/opencode-review/runner (bwrap, per-instance
# home, cron-supervised by `runner-ctl ensure`). Already registered instances are left alone.
# EXTRA_LABELS (comma-separated, per role below via TEAMUP_TRANSITION=1) keeps the labels the
# workflows on main used before a rename, so jobs keep running until the rename merges; drop them
# afterwards with `gh api -X DELETE repos/<repo>/actions/runners/<id>/labels/<label>`.
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin
REPO=${TEAMUP_REPO:-Jianyu010/TeamUp}
BASE=/home/jump/gh-runner
APP=/home/jump/opencode-review
SEED=$BASE/state/ppma-gate-1/home # a home with uv, Python 3.12 and the mirror's uv.toml
PKG=$(ls "$BASE"/.cache/actions-runner-linux-x64-*.tar.gz | sort -V | tail -1)
TRANSITION=${TEAMUP_TRANSITION:-0}

IFS= read -r token
[[ $token =~ ^[A-Z0-9]{20,64}$ ]] || { echo "stdin is not a runner registration token" >&2; exit 2; }

# name  kind  transitional-label (- for none)
#   kind: code (PR code; uv + Python 3.12), review (OpenCode reviewer), e2e (suites; uv + LLM
#   relay), trusted (main's scripts only)
ROLES=(
  "lint-01          code    teamup-gate"
  "architecture-01  code    teamup-gate"
  "linux-ut-01      code    teamup-gate"
  "linux-ut-02      code    teamup-gate"
  "linux-ut-03      code    teamup-gate"
  "linux-st-01      code    teamup-gate"
  "linux-st-02      code    teamup-gate"
  "linux-st-03      code    teamup-gate"
  "ai-review-01     review  opencode-review"
  "ai-review-02     review  opencode-review"
  "ai-review-03     review  opencode-review"
  "e2e-01           e2e     teamup-suite"
  "merge-gate-01    trusted teamup-merge"
  "merge-01         trusted teamup-merge"
  "gate-tick-01     trusted teamup-tick"
  "utility-01       trusted teamup-merge"
  "type-check-01    code    -"
  "secrets-01       code    -"
  "workflow-lint-01 code    -"
  "dependencies-01  code    -"
  "contract-01      code    -"
  "install-01       code    -"
  "install-02       code    -"
  "install-03       code    -"
  "linux-st-cross-01 code   -"
  "linux-st-cross-02 code   -"
  "linux-st-cross-03 code   -"
)

for row in "${ROLES[@]}"; do
  read -r name kind old <<<"$row"
  label=${name%-[0-9][0-9]}
  labels=$label
  [[ $TRANSITION == 1 && $old != - ]] && labels="$label,$old"
  inst=teamup-$name
  dir=$BASE/$inst
  home=$BASE/state/$inst/home
  if [[ -f $dir/.runner ]]; then
    echo "$inst: already registered"
    continue
  fi
  mkdir -p "$dir" "$home"
  [[ -f $dir/run.sh ]] || tar -xzf "$PKG" -C "$dir"
  case $kind in
    code | e2e)
      cp -a "$SEED/.local" "$home/"
      mkdir -p "$home/.config" && cp -a "$SEED/.config/uv" "$home/.config/"
      {
        echo "LANG=C.UTF-8"
        echo "PATH=/home/jump/.local/bin:/usr/local/bin:/usr/bin:/bin"
        echo "UV_PYTHON_DOWNLOADS=never"
      } >"$dir/.env"
      if [[ $kind == e2e ]]; then
        {
          echo "TEAMUP_OPENCODE=/home/jump/opencode-review/opencode/node_modules/.bin/opencode"
          echo "TEAMUP_LLM_BASE_URL=http://127.0.0.1:18081/v1"
        } >>"$dir/.env"
      fi
      ;;
    review)
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
    --name "$name" --labels "$labels" --work _work >/dev/null
  "$APP/runner/runner-ctl" start "$inst"
  echo "$inst: registered as $name with labels $labels"
done
