"""Coded errors: stable codes, categories, remediation hints, to_dict (Phase 3.9.2)."""

from __future__ import annotations

import inspect

import colabctl.errors as errmod
from colabctl.errors import (
    AcceleratorUnavailableError,
    AuthError,
    CLIError,
    ColabctlError,
    ExecutionError,
    QuotaExceededError,
    ScopeError,
)


def test_base_to_dict_shape():
    assert ColabctlError("boom").to_dict() == {
        "error": "ColabctlError",
        "code": "COLABCTL_ERROR",
        "category": "general",
        "message": "boom",
    }


def test_codes_categories_and_remediation():
    d = QuotaExceededError("out of compute units").to_dict()
    assert d["code"] == "QUOTA_EXCEEDED" and d["category"] == "allocation"
    assert "remediation" in d and "--allow" in d["remediation"]


def test_scope_overrides_code_keeps_auth_category():
    e = ScopeError("missing colaboratory scope")
    assert isinstance(e, AuthError)
    assert e.code == "SCOPE" and e.category == "auth" and e.remediation


def test_accelerator_unavailable_carries_field():
    d = AcceleratorUnavailableError("no A100 on this tier", accelerator="A100").to_dict()
    assert d["code"] == "ACCELERATOR_UNAVAILABLE" and d["accelerator"] == "A100"


def test_cli_error_carries_returncode():
    assert CLIError("failed", returncode=2, argv=["colab", "new"]).to_dict()["returncode"] == 2


def test_execution_error_carries_ename():
    d = ExecutionError("kernel raised", ename="ValueError", evalue="bad input").to_dict()
    assert d["ename"] == "ValueError" and d["evalue"] == "bad input"
    assert d["category"] == "execution"


def test_every_error_class_has_a_unique_upper_code():
    classes = [
        c
        for _n, c in inspect.getmembers(errmod, inspect.isclass)
        if issubclass(c, ColabctlError) and c is not ColabctlError
    ]
    codes = [c.code for c in classes]
    assert all(code.isupper() and code for code in codes)  # SCREAMING_SNAKE, non-empty
    dupes = {code for code in codes if codes.count(code) > 1}
    assert not dupes, f"duplicate error codes across classes: {dupes}"
