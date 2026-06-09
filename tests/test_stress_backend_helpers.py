"""Adversarial tests for backend script builders (injection safety) + status extractors."""

from __future__ import annotations

import shlex

from hypothesis import given
from hypothesis import strategies as st

from colabctl.backends.base import JobSpec
from colabctl.backends.hf_backend import _build_command as hf_build
from colabctl.backends.hf_backend import _extract_stage
from colabctl.backends.kaggle_backend import _build_script as kaggle_build
from colabctl.backends.kaggle_backend import _extract_status
from colabctl.backends.runpod_backend import _build_script as runpod_build
from colabctl.backends.runpod_backend import _pod_status

# Code samples with shell/python metacharacters that must NOT break out of quoting.
_NASTY_CODE = [
    "print('hi')",
    "import os; os.system('rm -rf /')",  # must stay inside the quoted -c arg
    'x = "$(whoami)"',
    "a = 1 && echo pwned",
    "s = '`backtick`'",
    "print('quote\\'s')",
    "multi\nline\ncode",
]


# --- kaggle builds Python; it must always compile ---------------------------


@given(reqs=st.lists(st.text(max_size=20), max_size=5))
def test_kaggle_script_compiles_with_arbitrary_requirements(reqs):
    spec = JobSpec(code="result = 1 + 1", requirements=reqs)
    compile(kaggle_build(spec), "<kaggle>", "exec")


def test_kaggle_script_requirements_are_repr_escaped():
    spec = JobSpec(code="x = 1", requirements=["evil'; import os", "normal-pkg"])
    script = kaggle_build(spec)
    compile(script, "<kaggle>", "exec")  # the injection attempt stays a string literal


# --- hf + runpod build shell; quoting must round-trip -----------------------


def _shlex_after_dash_c(tokens: list[str]) -> str:
    i = tokens.index("-c")
    return tokens[i + 1]


@given(code=st.text(min_size=1, max_size=80))
def test_hf_command_quotes_code_safely(code):
    spec = JobSpec(code=code)
    bash, dash_lc, inner = hf_build(spec)
    assert [bash, dash_lc] == ["bash", "-lc"]
    assert _shlex_after_dash_c(shlex.split(inner)) == code  # exact round-trip


@given(code=st.text(min_size=1, max_size=80))
def test_runpod_command_quotes_code_safely(code):
    inner = runpod_build(JobSpec(code=code))
    assert _shlex_after_dash_c(shlex.split(inner)) == code


def test_nasty_code_does_not_escape_quoting():
    for code in _NASTY_CODE:
        hf_inner = hf_build(JobSpec(code=code))[2]
        assert _shlex_after_dash_c(shlex.split(hf_inner)) == code
        rp_inner = runpod_build(JobSpec(code=code))
        assert _shlex_after_dash_c(shlex.split(rp_inner)) == code


def test_hf_with_requirements_still_round_trips_code():
    spec = JobSpec(code="print('x')", requirements=["torch", "weird; pkg"])
    inner = hf_build(spec)[2]
    tokens = shlex.split(inner)
    assert "pip" in tokens and "install" in tokens
    assert _shlex_after_dash_c(tokens) == "print('x')"


# --- status extractors: never crash on odd shapes ---------------------------


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_kaggle_extract_status_shapes():
    assert _extract_status(_Obj(status="complete")) == "complete"
    assert _extract_status({"status": "running"}) == "running"
    assert isinstance(_extract_status({}), str)  # no status key
    assert isinstance(_extract_status(None), str)
    assert isinstance(_extract_status("raw-string"), str)


def test_hf_extract_stage_shapes():
    assert _extract_stage(_Obj(status=_Obj(stage="RUNNING"))) == "RUNNING"
    assert _extract_stage(_Obj(stage="COMPLETED")) == "COMPLETED"
    assert isinstance(_extract_stage(_Obj()), str)  # nothing present
    assert isinstance(_extract_stage(None), str)


def test_runpod_pod_status_shapes():
    assert _pod_status({"desiredStatus": "RUNNING"}) == "RUNNING"
    assert _pod_status(_Obj(desiredStatus="EXITED")) == "EXITED"
    assert isinstance(_pod_status({}), str)
    assert isinstance(_pod_status(_Obj()), str)
    assert isinstance(_pod_status(None), str)
