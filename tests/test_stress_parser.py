"""Adversarial tests for the CLI stdout parser."""

from __future__ import annotations

import pytest

from colabctl.errors import (
    AcceleratorUnavailableError,
    ParseError,
    QuotaExceededError,
    ScopeError,
    TooManyAssignmentsError,
)
from colabctl.models import Accelerator, SessionStatus, Variant
from colabctl.transport.cli import parser


def test_busy_status_with_pipe_in_filename():
    line = "[s] ep-1 | Hardware: A100 | Variant: GPU | Status: BUSY (a | b.py)"
    info = parser.parse_session_line(line)
    assert info.status is SessionStatus.BUSY
    assert info.running == "a | b.py"


def test_status_trailing_whitespace_and_cpu():
    info = parser.parse_session_line("[c] ep | Hardware: CPU | Variant: DEFAULT | Status: IDLE   ")
    assert info.accelerator is Accelerator.NONE
    assert info.variant is Variant.DEFAULT
    assert info.status is SessionStatus.IDLE


def test_empty_name_is_allowed():
    info = parser.parse_session_line("[] ep | Hardware: T4 | Variant: GPU")
    assert info is not None
    assert info.name == ""


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "[colab] Session READY.",
        "[colab] Uploaded 'a' to 'b'",
        "[s] ep | Hardware: T4",  # no Variant → not a session line
        "random noise | Hardware: T4 | Variant: GPU",  # no [name] prefix
        "sample_data/",
    ],
)
def test_non_session_lines_return_none(line):
    assert parser.parse_session_line(line) is None


def test_unknown_hardware_and_variant_are_contract_drift():
    with pytest.raises(ParseError):
        parser.parse_session_line("[s] ep | Hardware: B200 | Variant: GPU | Status: IDLE")
    with pytest.raises(ParseError):
        parser.parse_session_line("[s] ep | Hardware: T4 | Variant: FOO | Status: IDLE")


def test_status_output_ignores_orphan_last_execution_line():
    # A Last-Execution line with no preceding session must not crash or attach.
    text = (
        "  Last Execution: orphan.py at 00:00\n"
        "[s] ep | Hardware: T4 | Variant: GPU | Status: IDLE\n"
        "  Last Execution: real.py at 01:00"
    )
    sessions = parser.parse_status_output(text)
    assert len(sessions) == 1
    assert sessions[0].last_execution == "real.py at 01:00"


def test_parse_new_output_multiple_creating_lines():
    text = (
        "[colab] Creating session 'a'...\n[colab] Creating session 'b'...\n[colab] Session READY."
    )
    name, ready = parser.parse_new_output(text)
    assert name == "b"  # last one wins
    assert ready is True


def test_sessions_output_mixed_lines():
    text = (
        "[colab] some banner\n"
        "[s1] ep1 | Hardware: T4 | Variant: GPU\n"
        "garbage\n"
        "[s2] ep2 | Hardware: A100 | Variant: GPU\n"
    )
    sessions = parser.parse_sessions_output(text)
    assert [s.name for s in sessions] == ["s1", "s2"]


@pytest.mark.parametrize(
    "blob,exc",
    [
        ("[colab] Backend rejected accelerator 'A100'. ...", AcceleratorUnavailableError),
        ("error: TooManyAssignments", TooManyAssignmentsError),
        ("body=[7,...SCOPE_NOT_PERMITTED...]", ScopeError),
        ("QUOTA_EXCEEDED_USAGE_TIME", QuotaExceededError),
    ],
)
def test_raise_for_known_errors(blob, exc):
    with pytest.raises(exc):
        parser.raise_for_known_errors(stdout="", stderr=blob, returncode=1, argv=["x"])


def test_raise_for_known_errors_clean_is_noop():
    parser.raise_for_known_errors(
        stdout="[colab] Session READY.", stderr="", returncode=0, argv=["new"]
    )
