"""Notebook execution adapter — run a full ``.ipynb`` with parameter injection.

papermill-style: inject a parameters cell (after the ``parameters``-tagged cell, or at
the top), then execute — either **cell-by-cell on a live session** (per-cell typed
outputs) or as **one script on a batch backend**. Notebooks are read as nbformat JSON,
so this has no hard dependency on papermill/nbclient; it runs *on colabctl's own
transports/backends* (the point: execute the notebook on a remote GPU, not locally).
"""

from __future__ import annotations

import copy
import json
import keyword
from pathlib import Path
from typing import Any

from colabctl.backends.base import Backend, JobResult, JobSpec
from colabctl.errors import ConfigurationError
from colabctl.models import Accelerator, ExecutionResult
from colabctl.sdk.client import ColabSession

_PARAM_TAG = "parameters"
_INJECTED_TAG = "injected-parameters"


def load_notebook(path: str | Path) -> dict[str, Any]:
    """Load a ``.ipynb`` as an nbformat dict."""
    return json.loads(Path(path).read_text())  # type: ignore[no-any-return]


def code_cells(nb: dict[str, Any]) -> list[str]:
    """Return the source of each non-empty code cell as a string."""
    cells: list[str] = []
    for cell in nb.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        src = cell.get("source") or ""  # nbformat: str | list[str]; tolerate null/missing
        text = "".join(str(line) for line in src) if isinstance(src, list) else str(src)
        if text.strip():
            cells.append(text)
    return cells


def inject_parameters(nb: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``nb`` with a parameters cell injected (papermill semantics)."""
    if not parameters:
        return nb
    for key in parameters:
        if not isinstance(key, str) or not key.isidentifier() or keyword.iskeyword(key):
            raise ConfigurationError(
                f"Parameter name {key!r} is not a valid Python identifier; "
                "it cannot be injected as a notebook parameter."
            )
    nb = copy.deepcopy(nb)
    cells = nb.setdefault("cells", [])
    injected = {
        "cell_type": "code",
        "metadata": {"tags": [_INJECTED_TAG]},
        "execution_count": None,
        "outputs": [],
        "source": [f"{key} = {value!r}\n" for key, value in parameters.items()],
    }
    tagged = next(
        (i for i, c in enumerate(cells) if _PARAM_TAG in (c.get("metadata", {}).get("tags") or [])),
        None,
    )
    cells.insert(0 if tagged is None else tagged + 1, injected)
    return nb


def notebook_to_script(nb: dict[str, Any], parameters: dict[str, Any] | None = None) -> str:
    """Flatten a (parameterized) notebook's code cells into one script."""
    return "\n\n".join(code_cells(inject_parameters(nb, parameters or {})))


async def run_notebook(
    session: ColabSession,
    path: str | Path,
    *,
    parameters: dict[str, Any] | None = None,
    stop_on_error: bool = True,
    timeout: float | None = None,
) -> list[ExecutionResult]:
    """Run a notebook **cell-by-cell** on a live session; return per-cell results.

    Stops at the first failing cell when ``stop_on_error`` (papermill's default).
    """
    nb = inject_parameters(load_notebook(path), parameters or {})
    results: list[ExecutionResult] = []
    for cell in code_cells(nb):
        result = await session.run(cell, timeout=timeout)
        results.append(result)
        if stop_on_error and not result.ok:
            break
    return results


async def run_notebook_job(
    backend: Backend,
    path: str | Path,
    *,
    parameters: dict[str, Any] | None = None,
    accelerator: Accelerator = Accelerator.T4,
    requirements: list[str] | None = None,
) -> JobResult:
    """Run a whole notebook as a single batch job on a backend."""
    script = notebook_to_script(load_notebook(path), parameters)
    return await backend.run(
        JobSpec(code=script, accelerator=accelerator, requirements=requirements or [])
    )
