#!/usr/bin/env bash
# Install the static-check tools into TeamUp runner homes on hp-laptop (docs/ci/README.md, the
# contract table: `2 · Secrets`, `2 · Workflow Lint`, `2 · Dependencies`).
#
#   bash install-tools.sh teamup-secrets-01 teamup-workflow-lint-01 teamup-dependencies-01
#
# Arguments are instance names (the box-local directory names, with the `teamup-` prefix). Each
# gets, in its `home/.local/bin`:
#   - gitleaks, actionlint, shellcheck: pinned linux-x64 binaries copied from ~/gh-runner/tools/.
#     github.com downloads from the box are unreliable, so fetch them on a Mac, check the sha256
#     below, and scp them to ~/gh-runner/tools/ first:
#       gitleaks   https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz
#       actionlint https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz
#       shellcheck https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.linux.x86_64.tar.xz
#   - zizmor and pip-audit: `uv tool install` through the Tsinghua mirror, run inside the
#     instance's bwrap sandbox so the tool virtualenvs carry the in-sandbox paths (/home/jump/...).
# Re-running is safe: binaries are overwritten, uv tools are reinstalled at the pinned versions.
set -euo pipefail

TOOLS=$HOME/gh-runner/tools
STATE=$HOME/gh-runner/state
SANDBOX=$HOME/opencode-review/runner/runner-sandbox
MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple
ZIZMOR_VERSION=1.33.1
PIP_AUDIT_VERSION=2.10.0

# sha256 of the binaries as extracted from the release archives
declare -A SHA256=(
  [gitleaks]=88f91962aa2f93ac6ab281d553b9e125f5197bbbce38f9f2437f7299c32e5509   # v8.30.1
  [actionlint]=c872d6db8c6bf83a8eaa704fc93999f027d55dffbc63b8a6abdccb47df5f4cd4 # v1.7.12
  [shellcheck]=4da528ddb3a4d1b7b24a59d4e16eb2f5fd960f4bd9a3708a15baddbdf1d5a55b # v0.11.0
)

(($# > 0)) || { echo "usage: $0 <instance>..." >&2; exit 2; }

for name in "${!SHA256[@]}"; do
  [[ -x $TOOLS/$name ]] || { echo "missing $TOOLS/$name (see the header)" >&2; exit 1; }
  echo "${SHA256[$name]}  $TOOLS/$name" | sha256sum -c --quiet - \
    || { echo "$name does not match its pinned sha256" >&2; exit 1; }
done

for inst in "$@"; do
  home=$STATE/$inst/home
  [[ -d $home ]] || { echo "no runner home for $inst ($home)" >&2; exit 1; }
  mkdir -p "$home/.local/bin"
  for name in "${!SHA256[@]}"; do
    install -m 0755 "$TOOLS/$name" "$home/.local/bin/$name"
  done
  "$SANDBOX" "$inst" env PATH=/home/jump/.local/bin:/usr/local/bin:/usr/bin:/bin \
    UV_DEFAULT_INDEX=$MIRROR UV_PYTHON_DOWNLOADS=never \
    bash -euc "
      uv tool install --force --python 3.12 'zizmor==$ZIZMOR_VERSION'
      uv tool install --force --python 3.12 'pip-audit==$PIP_AUDIT_VERSION'
      gitleaks version; actionlint -version | head -1; shellcheck --version | sed -n 2p
      zizmor --version; pip-audit --version
    "
  echo "installed into $inst"
done
