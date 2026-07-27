#!/usr/bin/env bash
# Replays Protocol III task 36 in an isolated local-browser run and fails unless
# the agent produces a successful task artifact.  The output directory is kept
# so a failing trajectory can be inspected.
set -euo pipefail

repo_dir=$(cd "$(dirname "$0")/.." && pwd)
output_dir=${WEBRETRIEVER_REPRO_OUTPUT:-$(mktemp -d /tmp/webretriever-task36.XXXXXX)}

printf 'Task 36 reproduction output: %s\n' "$output_dir"
cd "$repo_dir"
conda run --no-capture-output -n Browser-Use python run_webretriever.py \
  --input data/data/protocol3.json \
  --output "$output_dir" \
  --task-index 36 \
  --local-browser \
  --rerun-failed \
  --max-concurrency 1 \
  --max-steps 8 \
  --task-timeout 240

result_path=$(find "$output_dir" -name result.json -type f -print -quit)
test -n "$result_path"
jq -e '
  .status == "SUCCESS"
  and (.agent_answer | type == "string" and length > 0)
  and (.evidence | type == "array" and length > 0)
' "$result_path" >/dev/null
printf 'Task 36 succeeded: %s\n' "$result_path"
