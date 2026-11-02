#!/usr/bin/env bash
# Runs scripts/bench.py against the last release and the checkout, in turn,
# baton-spec's "repeats" times each, then prints the comparison.
#
#   scripts/bench.sh [OUT_DIR]
#
# The last release is the newest tag before HEAD; BENCH_AGAINST=X.Y.Z picks
# another. Both sides get the same locked versions of the dependencies they share.
set -euo pipefail

out="${1:-$(dirname "$0")/../bench-results}"
mkdir -p "$out"
out="$(cd "$out" && pwd)"
cd "$(dirname "$0")/.."
against="${BENCH_AGAINST:-$(git describe --tags --abbrev=0 HEAD^)}"
against="${against#v}"
repeats="$(python3 -c 'import json; print(json.load(open("baton-spec/bench/bench.json"))["repeats"])')"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

uv export --locked --quiet --no-dev --extra mcp --no-emit-project --no-hashes -o "$work/requirements.txt"
for side in published checkout; do
  uv venv --quiet "$work/$side"
  uv pip install --quiet --python "$work/$side" -r "$work/requirements.txt"
done
# Resolved within the lock, so a dependency the release has and the checkout
# dropped (0.9.3's httpx) is installed for it alone.
uv pip install --quiet --python "$work/published" -c "$work/requirements.txt" "baton-sdk==$against"
uv pip install --quiet --python "$work/checkout" --no-deps .

for n in $(seq "$repeats"); do
  for side in published checkout; do
    echo "run $n of $repeats: $side" >&2
    "$work/$side/bin/python" scripts/bench.py --source "$side" --out "$out/$side-$n.json" 2>"$out/$side-$n.log"
  done
done
python3 baton-spec/scripts/compare_bench.py "$out"/published-*.json "$out"/checkout-*.json | tee "$out/compare.md"
