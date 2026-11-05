#!/usr/bin/env bash
# Submit a Python and a JavaScript program and print the results. Requires curl and jq.
set -euo pipefail
API=${API:-http://localhost:8000/api/v1}
run() {
  id=$(curl -s -X POST "$API/executions" -H 'Content-Type: application/json' -d "$1" | jq -r .execution_id)
  for _ in $(seq 1 40); do
    out=$(curl -s "$API/executions/$id"); st=$(echo "$out" | jq -r .status)
    case "$st" in QUEUED|RUNNING) sleep 0.5;; *) echo "$out" | jq '{status,exit_code,stdout,stderr,execution_duration_ms,error_type}'; return;; esac
  done
}
run '{"language":"python","source_code":"print(input().upper())","stdin":"hello codeforge\n"}'
run '{"language":"javascript","source_code":"console.log([1,2,3].map(x => x*x))"}'
run '{"language":"python","source_code":"while True: pass"}'
