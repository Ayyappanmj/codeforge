# Failure handling and recovery

**Delivery is at-least-once. Execution is NOT exactly-once.** What the system guarantees is *idempotent result
recording*: a result can only be written while the row is `RUNNING`, so a delayed or duplicate message can never
overwrite a terminal state, and a terminal execution is never run again. If a worker dies mid-run, the job may
run a second time (bounded by `MAX_ATTEMPTS`); submitted code with side effects outside the sandbox cannot exist
(no network), but CPU work is repeated.

| Window | What happens | Mechanism | Test |
|---|---|---|---|
| A. API saved, crashed before publishing | Nothing to lose: the outbox row was committed with the execution. Publisher sends it later. | Transactional outbox | `test_outbox_recovers_after_publish_failure` (SQL, needs SQLAlchemy) |
| A'. RabbitMQ down | Publish fails, row gets exponential backoff (max 60s) and stays pending. API keeps accepting submissions. | Outbox + backoff | same |
| B. Worker dies mid-run | Message was un-acked -> redelivered, sees `RUNNING` -> acked as in-progress duplicate. Sweeper later requeues rows `RUNNING` longer than `timeout + grace`; after `MAX_ATTEMPTS` the row becomes `FAILED/WORKER_LOST`. | Sweeper | `test_sweeper_requeues_stale_running_then_fails...` (SQL) |
| C. Result recorded, crash before ack | Redelivery finds a terminal row -> ack, no re-run. | Guarded claim | `test_duplicate_delivery_after_completion_does_not_rerun` (**ran**) |
| D. Duplicate delivery | Only one claimer wins the `QUEUED->RUNNING` CAS; others ack. | Guarded claim | `test_concurrent_deliveries_run_exactly_once` (**ran**, in-memory store) |
| E. PostgreSQL unavailable | On claim: message goes to the retry queue (TTL delay, bounded by `MAX_MESSAGE_RETRIES`, then DLQ). On record: 3 in-process retries with backoff; if still down the row stays `RUNNING` and the sweeper requeues it. | Retry queue + sweeper | `test_db_*` in `test_processor_failures.py` (**ran**, fake store) |
| F. Container timeout | Runner kills the container (`docker kill`), records `TIMED_OUT`, removes the container in `finally`. | Runner | `test_infinite_loop_times_out_and_is_killed` (**ran** against fake docker; real-Docker variant not run here) |

## Poison messages and dead-lettering
Malformed JSON or an unknown execution id -> published to `codeforge.executions.dead` (reason header), then acked,
so it cannot loop. A message that needs retrying more than `MAX_MESSAGE_RETRIES` times is dead-lettered too.
Rows stranded by a dead-lettered message are picked up by the sweeper (stale `QUEUED` with no pending outbox ->
new outbox event, up to `MAX_ENQUEUES`, then `FAILED/QUEUE_EXHAUSTED`).

## Acknowledgement policy
The worker acks only after (1) the terminal state is durably written, or (2) the message has been re-published
to the retry/dead queue with confirms. Docker failures (`docker run` exit 125, missing binary) are infrastructure
errors: the execution is requeued via a *new outbox event* with a delay (so the broker never hot-loops), up to
`MAX_ATTEMPTS`, then `FAILED/INFRASTRUCTURE_ERROR`.

## Cancellation
- `QUEUED`: atomic CAS to `CANCELLED`; a later delivery sees a terminal row and is acked.
- `RUNNING`: sets `cancel_requested`; the worker polls it every 0.5s, kills the container and records `CANCELLED`.
  If the program finishes on its own first, the real result is kept and a later cancel returns `409`.
- Cancel racing completion can never flip a finished job (`finish` only updates `RUNNING` rows).

## Graceful shutdown
SIGTERM sets a flag; the worker finishes the current job, stops consuming and closes the connection
(compose `stop_grace_period: 30s`).

## Known gaps
- **No automatic orphan reaper.** If a worker is SIGKILLed mid-run, its sandbox container is not removed by
  anyone. Orphans carry the label `codeforge.sandbox=1`; remove them with
  `docker rm -f $(docker ps -aq --filter label=codeforge.sandbox=1)` (only when no worker is mid-run).
  A reaper is listed under future work.
