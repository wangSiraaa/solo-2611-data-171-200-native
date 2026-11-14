#!/usr/bin/env bash
# Execute every fixture notebook independently: one fresh kernel per run.
# Outputs land in results/ and are never written back into fixtures/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
mkdir -p results

for name in analysis failure timeout; do
  echo "=================================================================="
  echo "running fixtures/$name.ipynb"
  echo "=================================================================="
  "$PY" -m nbexec run \
    --notebook "fixtures/$name.ipynb" \
    --output "results/$name.ipynb" \
    --record "results/$name.record.json" \
    --parameters fixtures/parameters.json \
    --plan fixtures/plan.json \
    --working-directory "$ROOT"
  echo
done

# Extra fixture: interactive stdin must fail the cell explicitly instead of hanging.
echo "=================================================================="
echo "running fixtures/stdin.ipynb (stdin rejection case)"
echo "=================================================================="
"$PY" -m nbexec run \
  --notebook fixtures/stdin.ipynb \
  --output results/stdin.ipynb \
  --record results/stdin.record.json \
  --plan fixtures/plan.json \
  --working-directory "$ROOT"
