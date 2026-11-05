"""RabbitMQ topology and publishing helpers (pika, blocking)."""
from __future__ import annotations

import json

import pika
from pika.exceptions import AMQPError

EXCHANGE = "codeforge"
DLX = "codeforge.dlx"
RETRY_EXCHANGE = "codeforge.retry"
QUEUE = "codeforge.executions"
RETRY_QUEUE = "codeforge.executions.retry"
DEAD_QUEUE = "codeforge.executions.dead"
RK = "execute"
RK_DEAD = "dead"
RK_RETRY = "retry"
RETRY_HEADER = "x-cf-retry-count"


def connect(url: str, heartbeat: int = 60) -> pika.BlockingConnection:
    params = pika.URLParameters(url)
    params.heartbeat = heartbeat
    params.blocked_connection_timeout = 30
    params.socket_timeout = 10
    return pika.BlockingConnection(params)


def declare_topology(channel, retry_delay_ms: int) -> None:
    """Idempotent. Main queue dead-letters rejected messages to the DLQ; the retry queue holds
    messages for retry_delay_ms and then dead-letters them back to the main exchange."""
    channel.exchange_declare(EXCHANGE, "direct", durable=True)
    channel.exchange_declare(DLX, "direct", durable=True)
    channel.exchange_declare(RETRY_EXCHANGE, "direct", durable=True)
    channel.queue_declare(DEAD_QUEUE, durable=True)
    channel.queue_bind(DEAD_QUEUE, DLX, RK_DEAD)
    channel.queue_declare(QUEUE, durable=True, arguments={
        "x-dead-letter-exchange": DLX, "x-dead-letter-routing-key": RK_DEAD})
    channel.queue_bind(QUEUE, EXCHANGE, RK)
    channel.queue_declare(RETRY_QUEUE, durable=True, arguments={
        "x-message-ttl": retry_delay_ms,
        "x-dead-letter-exchange": EXCHANGE, "x-dead-letter-routing-key": RK})
    channel.queue_bind(RETRY_QUEUE, RETRY_EXCHANGE, RK_RETRY)


def _props(headers=None) -> pika.BasicProperties:
    return pika.BasicProperties(delivery_mode=pika.DeliveryMode.Persistent,
                                content_type="application/json", headers=headers or {})


def publish_execution(channel, execution_id: str, correlation_id: str) -> None:
    """Publish with confirms + mandatory flag; raises if the broker did not accept/route it."""
    body = json.dumps({"execution_id": execution_id, "correlation_id": correlation_id}).encode()
    channel.basic_publish(EXCHANGE, RK, body, _props(), mandatory=True)


def publish_retry(channel, body: bytes, retry_count: int) -> None:
    channel.basic_publish(RETRY_EXCHANGE, RK_RETRY, body, _props({RETRY_HEADER: retry_count}),
                          mandatory=True)


def publish_dead(channel, body: bytes, reason: str) -> None:
    channel.basic_publish(DLX, RK_DEAD, body, _props({"x-cf-dead-reason": reason[:200]}),
                          mandatory=True)


__all__ = ["AMQPError", "connect", "declare_topology", "publish_execution", "publish_retry",
           "publish_dead"]
