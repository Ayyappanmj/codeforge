"""Outbox publisher + recovery sweeper (separate process from API and worker)."""
from __future__ import annotations

import logging
import signal
import time

from codeforge_shared import metrics, mq
from codeforge_shared.config import Settings
from codeforge_shared.logging_utils import configure_logging, log_event
from codeforge_shared.store import SqlStore, make_session_factory
from prometheus_client import start_http_server

log = logging.getLogger("codeforge.publisher")
_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True


def run_once(store: SqlStore, settings: Settings, publish_fn) -> int:
    n = store.publish_due_outbox(publish_fn, settings.outbox_batch_size)
    if n:
        metrics.OUTBOX_PUBLISH.labels("published").inc(n)
    return n


def run_sweep(store: SqlStore, settings: Settings) -> dict:
    counts = store.sweep(
        stale_running_seconds=settings.stale_running_seconds,
        stale_queued_seconds=settings.stale_queued_seconds,
        max_attempts=settings.max_attempts, max_enqueues=settings.max_enqueues)
    for kind, n in counts.items():
        if n:
            metrics.RECOVERIES.labels(kind).inc(n)
            log_event(log, logging.WARNING, "sweeper_recovered", kind=kind, count=n)
    return counts


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    start_http_server(settings.publisher_metrics_port)
    store = SqlStore(make_session_factory(settings.database_url))
    last_sweep = 0.0
    conn = None
    while not _stop:
        try:
            if conn is None or conn.is_closed:
                conn = mq.connect(settings.rabbitmq_url)
                ch = conn.channel()
                mq.declare_topology(ch, settings.retry_delay_ms)
                ch.confirm_delivery()
                log_event(log, logging.INFO, "publisher_connected")
            run_once(store, settings, lambda eid, cid, ch=ch: mq.publish_execution(ch, eid, cid))
            conn.process_data_events(time_limit=0)
            if time.monotonic() - last_sweep > 10:
                last_sweep = time.monotonic()
                run_sweep(store, settings)
        except Exception:  # noqa: BLE001 - keep the loop alive; the outbox row keeps its state
            metrics.OUTBOX_PUBLISH.labels("error").inc()
            metrics.QUEUE_FAILURES.labels("publish").inc()
            log_event(log, logging.WARNING, "publisher_cycle_failed", exc_info=True)
            try:
                if conn is not None and conn.is_open:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass
            conn = None
            time.sleep(2)
        time.sleep(settings.outbox_poll_seconds)


if __name__ == "__main__":
    main()
