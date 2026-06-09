"""Adversarial tests for routing, retry/backoff, and accelerator resolution."""

from __future__ import annotations

import pytest

from colabctl.backends.base import (
    Backend,
    BackendCapabilities,
    JobInfo,
    JobResult,
    JobSpec,
    JobState,
)
from colabctl.backends.router import BackendRouter
from colabctl.errors import (
    AcceleratorUnavailableError,
    ColabctlError,
    ConfigurationError,
    QuotaExceededError,
    TransportError,
)
from colabctl.models import Accelerator
from colabctl.observability import cap_timeout, retry_async
from colabctl.sdk.client import _resolve_accelerator


class FlakyBackend(Backend):
    """Backend that raises a configured error on run() and counts attempts."""

    def __init__(self, name: str, *, accels=None, error: Exception | None = None):
        self.name = name
        self._accels = accels if accels is not None else ["T4"]
        self._error = error
        self.run_calls = 0
        self.closed = False

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(name=self.name, accelerators=self._accels)

    async def submit(self, spec: JobSpec) -> JobInfo:
        return JobInfo(id="j", backend=self.name, state=JobState.PENDING)

    async def status(self, job_id: str) -> JobInfo:
        return JobInfo(id=job_id, backend=self.name, state=JobState.SUCCEEDED)

    async def logs(self, job_id: str) -> str:
        return ""

    async def result(self, job_id: str) -> JobResult:
        return JobResult(id=job_id, backend=self.name, state=JobState.SUCCEEDED)

    async def run(self, spec: JobSpec) -> JobResult:
        self.run_calls += 1
        if self._error is not None:
            raise self._error
        return JobResult(id="j", backend=self.name, state=JobState.SUCCEEDED, stdout=self.name)

    async def cancel(self, job_id: str) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


def _spec(acc: Accelerator = Accelerator.T4) -> JobSpec:
    return JobSpec(code="print(1)", accelerator=acc)


# --- router construction ----------------------------------------------------


def test_order_with_unknown_name_is_configuration_error():
    with pytest.raises(ConfigurationError):
        BackendRouter([FlakyBackend("a")], order=["a", "ghost"])


def test_duplicate_names_in_order_are_deduped():
    r = BackendRouter([FlakyBackend("a")], order=["a", "a", "a"])
    assert r._order == ["a"]


def test_omitted_backends_are_appended_not_dropped():
    r = BackendRouter([FlakyBackend("a"), FlakyBackend("b")], order=["b"])
    # 'a' was not in order but must remain reachable
    assert set(r._order) == {"a", "b"}
    assert r._order[0] == "b"


def test_prefer_unregistered_raises():
    r = BackendRouter([FlakyBackend("a")])
    with pytest.raises(ConfigurationError):
        r.candidates(_spec(), prefer="ghost")


# --- candidate selection ----------------------------------------------------


def test_candidates_filter_by_accelerator():
    r = BackendRouter(
        [FlakyBackend("t4only", accels=["T4"]), FlakyBackend("a100", accels=["A100"])]
    )
    names = [b.name for b in r.candidates(_spec(Accelerator.A100))]
    assert names == ["a100"]


def test_unconstrained_backend_supports_anything():
    r = BackendRouter([FlakyBackend("any", accels=[])])
    assert len(r.candidates(_spec(Accelerator.A100))) == 1


def test_no_supporting_backend_raises_on_select_and_run():
    r = BackendRouter([FlakyBackend("t4only", accels=["T4"])])
    with pytest.raises(ConfigurationError):
        r.select(_spec(Accelerator.A100))


async def test_no_supporting_backend_run_raises():
    r = BackendRouter([FlakyBackend("t4only", accels=["T4"])])
    with pytest.raises(ConfigurationError):
        await r.run(_spec(Accelerator.A100))


# --- failover ---------------------------------------------------------------


async def test_failover_to_next_on_colabctl_error():
    bad = FlakyBackend("bad", error=QuotaExceededError("out of quota"))
    good = FlakyBackend("good")
    r = BackendRouter([bad, good], order=["bad", "good"])
    result = await r.run(_spec())
    assert result.stdout == "good"
    assert bad.run_calls == 1 and good.run_calls == 1


async def test_no_fallback_reraises_first_error():
    bad = FlakyBackend("bad", error=AcceleratorUnavailableError("no a100"))
    good = FlakyBackend("good")
    r = BackendRouter([bad, good], order=["bad", "good"])
    with pytest.raises(AcceleratorUnavailableError):
        await r.run(_spec(), fallback=False)
    assert good.run_calls == 0  # never reached


async def test_all_failing_raises_aggregate():
    r = BackendRouter(
        [
            FlakyBackend("a", error=QuotaExceededError("q")),
            FlakyBackend("b", error=TransportError("t")),
        ],
        order=["a", "b"],
    )
    with pytest.raises(ColabctlError) as ei:
        await r.run(_spec())
    assert "a:" in str(ei.value) and "b:" in str(ei.value)


async def test_non_colabctl_error_does_not_failover():
    bad = FlakyBackend("bad", error=ValueError("not ours"))
    good = FlakyBackend("good")
    r = BackendRouter([bad, good], order=["bad", "good"])
    with pytest.raises(ValueError):
        await r.run(_spec())
    assert good.run_calls == 0  # a non-ColabctlError must propagate, not fail over


# --- retry_async ------------------------------------------------------------


async def test_retry_succeeds_after_transient_failures():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransportError("transient")
        return "ok"

    async def no_sleep(_):
        return None

    out = await retry_async(op, retries=3, sleep=no_sleep, jitter=lambda: 0.0)
    assert out == "ok"
    assert calls["n"] == 3


async def test_retry_gives_up_immediately_on_terminal_error():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise QuotaExceededError("terminal")

    async def no_sleep(_):
        return None

    with pytest.raises(QuotaExceededError):
        await retry_async(op, retries=5, sleep=no_sleep, jitter=lambda: 0.0)
    assert calls["n"] == 1  # never retried


async def test_retry_exhausts_and_reraises():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise TransportError("always")

    async def no_sleep(_):
        return None

    with pytest.raises(TransportError):
        await retry_async(op, retries=2, sleep=no_sleep, jitter=lambda: 0.0)
    assert calls["n"] == 3  # initial + 2 retries


async def test_retry_backoff_is_capped():
    delays: list[float] = []

    async def op():
        raise TransportError("x")

    async def record(d):
        delays.append(d)

    with pytest.raises(TransportError):
        await retry_async(
            op, retries=5, base_delay=1.0, max_delay=4.0, sleep=record, jitter=lambda: 0.0
        )
    assert delays == [1.0, 2.0, 4.0, 4.0, 4.0]  # 1,2,4,8→cap,16→cap


async def test_retry_zero_means_single_attempt():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise TransportError("x")

    with pytest.raises(TransportError):
        await retry_async(op, retries=0, sleep=lambda _: _noop(), jitter=lambda: 0.0)
    assert calls["n"] == 1


async def _noop():
    return None


# --- cap_timeout ------------------------------------------------------------


@pytest.mark.parametrize(
    "requested,maximum,expected",
    [(10, 100, 10), (100, 100, 100), (101, 100, 100), (0, 100, 0)],
)
def test_cap_timeout(requested, maximum, expected):
    assert cap_timeout(requested, maximum=maximum) == expected


# --- _resolve_accelerator ---------------------------------------------------


def test_resolve_explicit_accelerator_wins():
    assert _resolve_accelerator("a100", Accelerator.T4, default=Accelerator.NONE) is Accelerator.T4


@pytest.mark.parametrize("text,expected", [("t4", Accelerator.T4), ("A100", Accelerator.A100)])
def test_resolve_gpu_string_is_case_insensitive(text, expected):
    assert _resolve_accelerator(text, None, default=Accelerator.NONE) is expected


def test_resolve_unknown_gpu_raises_with_valid_list():
    with pytest.raises(ConfigurationError) as ei:
        _resolve_accelerator("rtx9090", None, default=Accelerator.NONE)
    assert "T4" in str(ei.value)


def test_resolve_falls_back_to_default():
    assert _resolve_accelerator(None, None, default=Accelerator.T4) is Accelerator.T4
