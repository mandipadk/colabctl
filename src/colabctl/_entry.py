"""Console-script launchers with friendly missing-extra errors.

The ``colabctl`` and ``colabctl-mcp`` entry points go through here so a *bare* install
(``pip install colabctl`` without the ``cli`` / ``mcp`` extra) prints a clear
"install this extra" message and exits non-zero — instead of dumping a raw ImportError
traceback (and, worse, exiting 0). The real modules are imported lazily so this file stays
importable with only the core dependencies.
"""

from __future__ import annotations

import sys
from typing import NoReturn


def _require_extra(extra: str, exc: ImportError, *, modules: tuple[str, ...]) -> NoReturn:
    """Exit friendly if ``exc`` is a known missing optional dependency; else re-raise.

    Only translate the error when the missing top-level module is one this extra provides —
    an unrelated ImportError (a real bug) is re-raised untouched, never masked.
    """
    missing = (exc.name or "").split(".")[0]
    if missing in modules:
        sys.stderr.write(
            f'colabctl: the "{extra}" extra is not installed (missing: {missing}).\n'
            f'  install it with:  pip install "colabctl[{extra}]"\n'
        )
        raise SystemExit(1)
    raise exc


def cli_main() -> None:
    try:
        from colabctl.cli import main
    except ImportError as exc:
        _require_extra("cli", exc, modules=("typer", "rich"))
    main()


def mcp_main() -> None:
    try:
        from colabctl.mcp_server import main
    except ImportError as exc:
        _require_extra("mcp", exc, modules=("mcp",))
    main()


__all__ = ["cli_main", "mcp_main"]
