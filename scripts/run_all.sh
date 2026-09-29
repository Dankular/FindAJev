#!/usr/bin/env bash
# Full benchmark: every registry model, sequentially (never in parallel: timings would contaminate each other).
# Usage: scripts/run_all.sh [threads]     needs: dotnet 8, python deps (requirements.txt), data/encoded/*.jsonl (tools/encode.py)
set -euo pipefail
cd "$(dirname "$0")/.."
T=${1:-$(nproc)}
dotnet build src/FindAJev.Bench -c Release -o bin -v q >/dev/null
for id in $(python3 tools/fetch.py); do
  python3 tools/fetch.py "$id" 2>/dev/null
  dotnet bin/FindAJev.Bench.dll run "$id" --threads "$T" || true
done
dotnet bin/FindAJev.Bench.dll rank
