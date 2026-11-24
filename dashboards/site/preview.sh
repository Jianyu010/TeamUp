#!/usr/bin/env sh
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
port=${1:-8000}
tmp=$(mktemp -d)

cleanup() {
  rm -rf "$tmp"
}
trap cleanup EXIT

cp -R "$here"/. "$tmp"/
rm -rf "$tmp/data" "$tmp/sample-data"
mkdir -p "$tmp/data"
cp "$here"/sample-data/*.jsonl "$tmp/data/"

printf 'Serving dashboards at http://localhost:%s\n' "$port"
cd "$tmp"
python3 -m http.server "$port"
