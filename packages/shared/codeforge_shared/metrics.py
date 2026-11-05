from prometheus_client import Counter, Gauge, Histogram

SUBMISSIONS = Counter("codeforge_submissions_total", "Accepted submissions", ["language"])
EXECUTIONS = Counter(
    "codeforge_executions_total", "Finished executions", ["language", "status", "error_type"]
)
EXECUTION_DURATION = Histogram(
    "codeforge_execution_duration_seconds",
    "Container wall-clock duration",
    ["language"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 15, 30),
)
WORKER_ACTIVE = Gauge("codeforge_worker_active_executions", "Executions running in this worker")
MESSAGES = Counter("codeforge_worker_messages_total", "Worker message outcomes", ["outcome"])
OUTBOX_PUBLISH = Counter("codeforge_outbox_publish_total", "Outbox publish results", ["result"])
QUEUE_FAILURES = Counter("codeforge_queue_failures_total", "Queue-related failures", ["stage"])
RECOVERIES = Counter("codeforge_recoveries_total", "Sweeper recoveries", ["kind"])
