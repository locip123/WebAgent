#!/usr/bin/env bash
# Official interface: bash scripts/run.sh <task_file> <output_dir> <cdp_url_1> ...
set -euo pipefail

if [ "$#" -lt 3 ]; then
  echo "Usage: bash scripts/run.sh <task_file> <output_dir> <cdp_url_1> [cdp_url_2 ...]" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "$script_dir/.." && pwd)"

if [ ! -f "$1" ]; then
  echo "Task file does not exist: $1" >&2
  exit 2
fi

# Keep vendored code self-contained and never echo evaluator CDP URLs or tokens.
export PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m browser_use.webretriever.submission "$@"
