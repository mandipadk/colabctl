"""Tests for JobSpec validation and JobState semantics."""

from __future__ import annotations

import pytest

from colabctl.backends.base import JobSpec, JobState


def test_job_state_terminal():
    assert JobState.SUCCEEDED.is_terminal
    assert JobState.FAILED.is_terminal
    assert JobState.CANCELLED.is_terminal
    assert not JobState.RUNNING.is_terminal
    assert not JobState.PENDING.is_terminal


def test_jobspec_requires_exactly_one_source():
    with pytest.raises(ValueError):
        JobSpec()  # neither
    with pytest.raises(ValueError):
        JobSpec(code="x=1", script_path="a.py")  # both
    assert JobSpec(code="x=1").resolved_code() == "x=1"


def test_jobspec_resolved_code_reads_file(tmp_path):
    f = tmp_path / "s.py"
    f.write_text("print('hi')")
    assert JobSpec(script_path=str(f)).resolved_code() == "print('hi')"
