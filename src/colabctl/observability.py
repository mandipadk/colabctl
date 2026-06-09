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
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

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


def configure_logging(level: int | str = logging.INFO, *, stream: bool = True) -> None:
    """Opt-in logging setup for apps/CLI. Idempotent."""
    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    logger.setLevel(level)
    if stream and not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)


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
