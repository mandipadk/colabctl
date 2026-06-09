"""Adversarial + property tests for the notebook adapter (pure functions)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from colabctl.errors import ConfigurationError
from colabctl.notebook import (
    code_cells,
    inject_parameters,
    notebook_to_script,
)


def _nb(cells):
    return {"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}


def _code(source, tags=None):
    cell = {"cell_type": "code", "source": source, "outputs": [], "execution_count": None}
    if tags is not None:
        cell["metadata"] = {"tags": tags}
    return cell


# --- code_cells -------------------------------------------------------------


def test_code_cells_handles_str_list_none_and_missing_source():
    nb = _nb(
        [
            _code("a = 1"),
            _code(["b = ", "2"]),
            _code(None),  # null source → skipped, never "None" literal
            {"cell_type": "code", "outputs": []},  # missing source → skipped
            _code("   "),  # whitespace-only → skipped
            {"cell_type": "markdown", "source": "# title"},  # not code → skipped
        ]
    )
    assert code_cells(nb) == ["a = 1", "b = 2"]


def test_code_cells_empty_notebook():
    assert code_cells({}) == []
    assert code_cells({"cells": []}) == []


def test_code_cells_list_with_non_string_entries():
    # Defensive: nbformat says str, but tolerate stray non-strings.
    assert code_cells(_nb([_code([1, "= x"])])) == ["1= x"]


# --- inject_parameters ------------------------------------------------------


def test_inject_empty_params_is_noop_identity():
    nb = _nb([_code("x = 1")])
    assert inject_parameters(nb, {}) is nb  # no copy when nothing to inject


def test_inject_at_top_when_no_tagged_cell():
    nb = _nb([_code("print(x)")])
    out = inject_parameters(nb, {"x": 5})
    assert out is not nb  # copied
    assert out["cells"][0]["metadata"]["tags"] == ["injected-parameters"]
    assert "x = 5" in "".join(out["cells"][0]["source"])
    # original untouched
    assert len(nb["cells"]) == 1


def test_inject_after_tagged_parameters_cell():
    nb = _nb([_code("# setup"), _code("x = 0", tags=["parameters"]), _code("print(x)")])
    out = inject_parameters(nb, {"x": 9})
    # injected immediately AFTER the tagged cell (index 2)
    assert out["cells"][2]["metadata"]["tags"] == ["injected-parameters"]
    assert "x = 9" in "".join(out["cells"][2]["source"])


@pytest.mark.parametrize("bad_key", ["my-var", "1x", "class", "for", "has space", "", "a.b"])
def test_inject_rejects_invalid_identifier(bad_key):
    with pytest.raises(ConfigurationError):
        inject_parameters(_nb([_code("pass")]), {bad_key: 1})


def test_inject_rejects_non_string_key():
    with pytest.raises(ConfigurationError):
        inject_parameters(_nb([_code("pass")]), {1: 2})


# --- generated script always compiles ---------------------------------------

_ident = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True).filter(
    lambda s: not __import__("keyword").iskeyword(s)
)
_json_scalar = st.one_of(
    st.integers(),
    st.text(max_size=20),
    st.booleans(),
    st.none(),
    st.lists(st.integers(), max_size=5),
    st.floats(allow_nan=False, allow_infinity=False),
)


@given(params=st.dictionaries(_ident, _json_scalar, max_size=6))
def test_injected_parameter_cell_always_compiles(params):
    nb = _nb([_code("result = 1")])
    script = notebook_to_script(nb, params)
    compile(script, "<nb>", "exec")


def test_notebook_to_script_joins_cells():
    nb = _nb([_code("a = 1"), _code("b = 2")])
    assert notebook_to_script(nb) == "a = 1\n\nb = 2"
