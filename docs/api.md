# API reference (base path `/api/v1`)

Errors always look like `{"error": {"code": "...", "message": "...", "details": [{"field","message"}]}}`.
Send `X-Request-ID` (`[A-Za-z0-9._-]{1,64}`) to correlate logs; it is echoed back and stored on the execution.

| Method & path | Description |
|---|---|
| `POST /executions` | Body `{"language","source_code","stdin"?}` -> `202 {"execution_id","status":"QUEUED"}`. `422` invalid/unsupported language, `413` source>64 KiB or stdin>16 KiB. |
| `GET /executions/{id}` | `execution_id, language, status, created_at, started_at, completed_at, execution_duration_ms, exit_code, stdout, stderr, output_truncated, error_type, source_code, stdin, cancel_requested, attempt_count`. `404` if unknown. |
| `GET /executions?page=1&page_size=20&status=&language=` | `{"items":[summary],"total","page","page_size"}`, newest first, `page_size<=100`. |
| `POST /executions/{id}/cancel` | `200` (was queued, now CANCELLED), `202` (running; worker will stop it), `409` already finished, `404`. |
| `GET /languages` | Supported language keys. |
| `GET /health/live` | Process liveness. |
| `GET /health/ready` | `200` when PostgreSQL is reachable, else `503`. RabbitMQ status is reported but non-fatal because the outbox buffers submissions. |
| `GET /metrics` (no `/api/v1` prefix) | Prometheus metrics. |

`error_type` values: `RUNTIME_ERROR, TIMEOUT, OUTPUT_LIMIT_EXCEEDED, MEMORY_LIMIT_EXCEEDED, CANCELLED,
INFRASTRUCTURE_ERROR, WORKER_LOST, QUEUE_EXHAUSTED`.

```bash
curl -s -X POST localhost:8000/api/v1/executions -H 'Content-Type: application/json' \
  -d '{"language":"python","source_code":"print(input().upper())","stdin":"hi\n"}'
curl -s localhost:8000/api/v1/executions/<id>
```
