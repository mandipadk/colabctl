"""Adversarial tests for the CLI subprocess transport's command + error paths."""

from __future__ import annotations

import pytest

from colabctl.errors import CLIError
from colabctl.models import Accelerator, RuntimeSpec
from colabctl.transport.cli.adapter import ColabCliTransport


def _transport_with(responses: dict[str, tuple[int, str, str]]) -> ColabCliTransport:
    """A transport whose _run is replaced by a dispatcher keyed on the subcommand."""
    t = ColabCliTransport()
    t._probed = True  # skip the version probe
    captured: list[list[str]] = []

    async def fake_run(args, *, stdin=None, timeout=None):
        captured.append(args)
        return responses.get(args[0], (0, "", ""))

    t._run = fake_run  # type: ignore[method-assign]
    t._captured = captured  # type: ignore[attr-defined]
    return t


# --- argument construction --------------------------------------------------


@pytest.mark.parametrize(
    "acc,expected",
    [
        (Accelerator.NONE, []),
        (Accelerator.T4, ["--gpu", "T4"]),
        (Accelerator.A100, ["--gpu", "A100"]),
        (Accelerator.V5E1, ["--tpu", "v5e1"]),
    ],
)
def test_accelerator_args(acc, expected):
    assert ColabCliTransport._accelerator_args(acc) == expected


def test_global_args_includes_auth_and_config():
    t = ColabCliTransport(auth="oauth", config_path="/tmp/c.toml")
    assert t._global_args() == ["--auth", "oauth", "--config", "/tmp/c.toml"]


def test_global_args_without_config():
    assert ColabCliTransport(auth="adc")._global_args() == ["--auth", "adc"]


# --- allocate ---------------------------------------------------------------


async def test_allocate_success_returns_status_info():
    t = _transport_with(
        {
            "new": (0, "[colab] Creating session 'X'...\n[colab] Session READY.", ""),
            "status": (0, "[X] ep1 | Hardware: T4 | Variant: GPU | Status: IDLE", ""),
        }
    )
    info = await t.allocate(RuntimeSpec(accelerator=Accelerator.T4, name="X"))
    assert info.name == "X"
    assert info.endpoint == "ep1"
    assert info.accelerator is Accelerator.T4


async def test_allocate_synthesizes_when_status_empty():
    t = _transport_with({"new": (0, "[colab] Session READY.", ""), "status": (0, "", "")})
    info = await t.allocate(RuntimeSpec(accelerator=Accelerator.T4, name="Y"))
    assert info.name == "Y"
    assert info.endpoint == ""  # synthesized minimal record
    assert info.accelerator is Accelerator.T4


async def test_allocate_nonzero_exit_raises():
    t = _transport_with({"new": (1, "", "some failure")})
    with pytest.raises(CLIError):
        await t.allocate(RuntimeSpec(accelerator=Accelerator.T4, name="Z"))


async def test_allocate_missing_ready_raises():
    t = _transport_with({"new": (0, "[colab] Creating session 'Z'...", "")})  # no READY
    with pytest.raises(CLIError):
        await t.allocate(RuntimeSpec(accelerator=Accelerator.T4, name="Z"))


# --- execute ----------------------------------------------------------------


async def test_execute_ok_captures_stdout():
    t = _transport_with({"exec": (0, "hello\n", "")})
    result = await t.execute("s", "print('hi')")
    assert result.ok
    assert result.stdout == "hello\n"


async def test_execute_nonzero_is_error_with_stderr():
    t = _transport_with({"exec": (1, "", "Traceback...")})
    result = await t.execute("s", "boom")
    assert not result.ok
    assert result.stderr == "Traceback..."


async def test_execute_forwards_on_output():
    t = _transport_with({"exec": (0, "out", "err")})
    seen = []
    await t.execute("s", "x", on_output=seen.append)
    assert [o.name for o in seen] == ["stdout", "stderr"]


# --- upload / download / stop error paths -----------------------------------


async def test_upload_nonzero_raises():
    t = _transport_with({"upload": (1, "", "denied")})
    with pytest.raises(CLIError):
        await t.upload("s", __file__, "/remote")  # type: ignore[arg-type]


async def test_upload_missing_sentinel_raises():
    t = _transport_with({"upload": (0, "nothing useful", "")})
    with pytest.raises(CLIError):
        await t.upload("s", __file__, "/remote")  # type: ignore[arg-type]


async def test_stop_nonzero_raises():
    t = _transport_with({"stop": (2, "", "no such session")})
    with pytest.raises(CLIError):
        await t.stop("s")


async def test_list_sessions_nonzero_raises():
    t = _transport_with({"sessions": (1, "", "auth error")})
    with pytest.raises(CLIError):
        await t.list_sessions()


# --- real subprocess failure: missing binary -> CLIError --------------------


async def test_missing_binary_raises_cli_error():
    t = ColabCliTransport(colab_bin="/nonexistent/colab-binary-xyz-123")
    # The one-time probe swallows its own failure; the real command then surfaces it.
    with pytest.raises(CLIError):
        await t.list_sessions()
