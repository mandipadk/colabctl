"""Logging + retry/backoff for colabctl.

Library convention: the package attaches a ``NullHandler`` and never configures
logging on import; applications (and the CLI) opt in via :func:`configure_logging`.
:func:`retry_async` is the one place transient-error backoff lives, so transports and
backends don't each reinvent it — and crucially it *does not* retry terminal errors
(quota exhausted, accelerator unavailable, too-many-assignments), which retrying would
only waste time and money on.
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime
import json as _json
import logging
import os
import random
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any, TypeVar

from colabctl.errors import (
    AcceleratorUnavailableError,
    QuotaExceededError,
    TooManyAssignmentsError,
    TransportError,
)

_T = TypeVar("_T")

_ROOT_LOGGER_NAME = "colabctl"

# Don't emit "No handlers could be found" warnings; apps configure handlers.
logging.getLogger(_ROOT_LOGGER_NAME).addHandler(logging.NullHandler())


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, e.g. ``get_logger("transport.native")``."""
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")


# --- correlation IDs (Phase 4.10.2) -----------------------------------------
# A context-local bag of ids (job_id/session_id/backend/trace_id) attached to every log line
# emitted while it's bound — so the operational log of a 12h cross-reassignment job is
# greppable by job_id end to end.

_correlation: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "colabctl_correlation", default=None
)


@contextmanager
def correlation_context(**fields: str | None) -> Iterator[None]:
    """Bind correlation ids onto every colabctl log line emitted within the ``with`` block."""
    merged = {**(_correlation.get() or {}), **{k: v for k, v in fields.items() if v is not None}}
    token = _correlation.set(merged)
    try:
        yield
    finally:
        _correlation.reset(token)


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation = _correlation.get() or {}
        return True


# --- structured (JSON) logging + an optional event sink (Phase 4.10.4) ------

#: An optional sink for structured log events (e.g. an OpenTelemetry exporter). colabctl takes
#: NO otel dependency — you set a callable that adapts the event dict. See :func:`set_event_sink`.
_event_sink: Callable[[dict[str, Any]], None] | None = None


def set_event_sink(sink: Callable[[dict[str, Any]], None] | None) -> None:
    """Forward every structured log event to ``sink`` (a thin hook; pass None to disable).

    The event dict carries ts/level/logger/message + any bound correlation ids — adapt it to
    an OTel span/log or any backend. This is a hook, not a tracing subsystem.
    """
    global _event_sink
    _event_sink = sink


def _record_payload(record: logging.LogRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ts": datetime.datetime.fromtimestamp(record.created, tz=datetime.UTC).isoformat(),
        "level": record.levelname,
        "logger": record.name,
        "message": record.getMessage(),
    }
    corr = getattr(record, "correlation", None)
    if corr:
        payload.update(corr)
    if record.exc_info:
        payload["exc"] = logging.Formatter().formatException(record.exc_info)
    return payload


class JsonFormatter(logging.Formatter):
    """One JSON object per log line: ts/level/logger/message + bound correlation ids."""

    def format(self, record: logging.LogRecord) -> str:
        return _json.dumps(_record_payload(record))


class _EventSinkHandler(logging.Handler):
    """Forwards each record's structured payload to the configured event sink (if any)."""

    def emit(self, record: logging.LogRecord) -> None:
        if _event_sink is not None:
            with suppress(Exception):  # a sink must never break logging
                _event_sink(_record_payload(record))


def configure_logging(
    level: int | str = logging.INFO, *, stream: bool = True, json_logs: bool | None = None
) -> None:
    """Opt-in logging setup for apps/CLI. Idempotent.

    ``json_logs`` (or ``COLABCTL_LOG_JSON=1``) emits one JSON object per line with bound
    correlation ids; otherwise a human line. A structured event-sink handler is always attached
    (it no-ops until :func:`set_event_sink` is called).
    """
    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    logger.setLevel(level)
    use_json = json_logs if json_logs is not None else bool(os.environ.get("COLABCTL_LOG_JSON"))
    if stream and not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        handler: logging.Handler = logging.StreamHandler()
        handler.addFilter(_CorrelationFilter())
        handler.setFormatter(
            JsonFormatter()
            if use_json
            else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    if not any(isinstance(h, _EventSinkHandler) for h in logger.handlers):
        sink_handler = _EventSinkHandler()
        sink_handler.addFilter(_CorrelationFilter())
        logger.addHandler(sink_handler)


#: Errors that are terminal — retrying them wastes time/money, so never retry.
NON_RETRYABLE: tuple[type[Exception], ...] = (
    QuotaExceededError,
    AcceleratorUnavailableError,
    TooManyAssignmentsError,
)

_log = get_logger("retry")


def cap_timeout(requested: int, *, maximum: int, label: str = "backend") -> int:
    """Spend guard: clamp a requested timeout to ``maximum``, logging if it bites.

    Paid backends bill per second, so an agent requesting a huge timeout is a real
    runaway-cost risk; this enforces a hard ceiling.
    """
    if requested > maximum:
        get_logger("spend").warning(
            "%s: capping requested timeout %ss to max %ss (spend guard)",
            label,
            requested,
            maximum,
        )
        return maximum
    return requested


async def retry_async(
    op: Callable[[], Awaitable[_T]],
    *,
    retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_on: tuple[type[Exception], ...] = (TransportError,),
    give_up_on: tuple[type[Exception], ...] = NON_RETRYABLE,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] | None = None,
) -> _T:
    """Run ``op`` with exponential backoff on ``retry_on`` errors.

    Immediately re-raises ``give_up_on`` errors (terminal: quota/entitlement/too-many).
    ``sleep`` and ``jitter`` are injectable for deterministic tests.
    """
    if jitter is None:
        jitter = lambda: random.uniform(0.0, base_delay / 2)  # noqa: E731
    attempt = 0
    while True:
        try:
            return await op()
        except give_up_on:
            raise
        except retry_on as exc:
            attempt += 1
            if attempt > retries:
                _log.debug("retry exhausted after %d attempts: %s", retries, exc)
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1))) + jitter()
            _log.debug("retry %d/%d after %.2fs: %s", attempt, retries, delay, exc)
            await sleep(delay)
