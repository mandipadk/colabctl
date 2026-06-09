"""Golden tests for the CLI parser, pinned to the live Phase 0 transcript.

Every string here is copied verbatim from a real `google-colab-cli` v0.5.7 run
against Colab Pro (see spikes/phase0-results.txt). If the CLI's output grammar
drifts, these break loudly — exactly the contract guard we want.
"""

from __future__ import annotations

import pytest

from colabctl.errors import AcceleratorUnavailableError, ParseError
from colabctl.models import Accelerator, SessionStatus, Variant
from colabctl.transport.cli import parser


def test_parse_status_line_idle():
    line = "[spk-t4] gpu-t4-s-kkb-usw1b0-2pd0ew59ycnb | Hardware: T4 | Variant: GPU | Status: IDLE"
    info = parser.parse_session_line(line)
    assert info is not None
    assert info.name == "spk-t4"
    assert info.endpoint == "gpu-t4-s-kkb-usw1b0-2pd0ew59ycnb"
    assert info.accelerator is Accelerator.T4
    assert info.variant is Variant.GPU
    assert info.status is SessionStatus.IDLE
    assert info.running is None


def test_parse_sessions_line_without_status():
    line = "[spk-t4] gpu-t4-s-kkb-usw1b0-2pd0ew59ycnb | Hardware: T4 | Variant: GPU"
    info = parser.parse_session_line(line)
    assert info is not None
    assert info.status is SessionStatus.UNKNOWN
    assert info.accelerator is Accelerator.T4


def test_parse_busy_status_captures_running_file():
    line = "[s1] ep-123 | Hardware: A100 | Variant: GPU | Status: BUSY (train.py)"
    info = parser.parse_session_line(line)
    assert info is not None
    assert info.status is SessionStatus.BUSY
    assert info.running == "train.py"
    assert info.accelerator is Accelerator.A100


def test_cpu_hardware_label_maps_to_none():
    line = "[cpu1] ep-x | Hardware: CPU | Variant: DEFAULT | Status: IDLE"
    info = parser.parse_session_line(line)
    assert info is not None
    assert info.accelerator is Accelerator.NONE
    assert info.variant is Variant.DEFAULT


def test_orphaned_session_name_question_mark():
    line = "[?] gpu-t4-s-orphan | Hardware: T4 | Variant: GPU"
    info = parser.parse_session_line(line)
    assert info is not None
    assert info.name == "?"


@pytest.mark.parametrize(
    "line",
    [
        "",
        "[colab] Session READY.",
        "[colab] Creating session 'spk-t4'...",
        "sample_data/",
        "random noise",
    ],
)
def test_non_session_lines_return_none(line: str):
    assert parser.parse_session_line(line) is None


def test_unknown_hardware_is_contract_drift():
    with pytest.raises(ParseError):
        parser.parse_session_line("[s] ep | Hardware: B200 | Variant: GPU | Status: IDLE")


def test_parse_sessions_output_full_block():
    text = "[spk-t4] gpu-t4-s-kkb-usw1b0-2pd0ew59ycnb | Hardware: T4 | Variant: GPU\n"
    sessions = parser.parse_sessions_output(text)
    assert len(sessions) == 1
    assert sessions[0].name == "spk-t4"


def test_parse_sessions_output_empty():
    assert parser.parse_sessions_output("[colab] No active sessions found on server.") == []


def test_parse_status_output_attaches_last_execution():
    text = (
        "[s1] ep-1 | Hardware: T4 | Variant: GPU | Status: IDLE\n"
        "  Last Execution: train.py | Cell: 3 at 2026-05-31 23:02\n"
    )
    sessions = parser.parse_status_output(text)
    assert len(sessions) == 1
    assert sessions[0].last_execution == "train.py | Cell: 3 at 2026-05-31 23:02"


def test_parse_new_output():
    text = "[colab] Creating session 'spk-t4'...\n[colab] Session READY.\n"
    name, ready = parser.parse_new_output(text)
    assert name == "spk-t4"
    assert ready is True


def test_parse_new_output_not_ready():
    name, ready = parser.parse_new_output("[colab] Creating session 'x'...\n")
    assert name == "x"
    assert ready is False


def test_parse_version():
    assert parser.parse_version("Version: 0.5.7") == "0.5.7"
    assert parser.parse_version("nope") is None


def test_upload_download_confirmations():
    assert parser.parse_upload_ok("[colab] Uploaded '/a/b.txt' to 'content/b.txt'")
    assert parser.parse_download_ok("[colab] Downloaded 'content/b.txt' to '/a/b.txt'")
    assert not parser.parse_upload_ok("[colab] something else")


def test_parse_terminated():
    assert parser.parse_terminated("[colab] Stopping session 'x'...\n[colab] Session terminated.")


def test_parse_ls_output_skips_colab_lines():
    text = ".config/\nsample_data/\nphase0_upload_test.txt\n[colab] note\n"
    assert parser.parse_ls_output(text) == [
        ".config/",
        "sample_data/",
        "phase0_upload_test.txt",
    ]


def test_raise_for_known_errors_accelerator():
    with pytest.raises(AcceleratorUnavailableError) as ei:
        parser.raise_for_known_errors(
            stdout="",
            stderr="[colab] Backend rejected accelerator 'A100'. You may not have quota...",
            returncode=1,
            argv=["new", "--gpu", "A100"],
        )
    assert ei.value.accelerator == "A100"


def test_raise_for_known_errors_noop_on_clean_output():
    # Should not raise on ordinary success output.
    parser.raise_for_known_errors(
        stdout="[colab] Session READY.", stderr="", returncode=0, argv=["new"]
    )
