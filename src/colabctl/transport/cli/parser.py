"""Parser for ``google-colab-cli`` human stdout.

The CLI has no ``--json`` mode, so this module is the single source of truth for
turning its printed lines into typed models. Every pattern here is grounded in
the live Phase 0 transcript and the CLI's ``session.py::_format_session_line``
(the CLI's own single source of truth for display lines). It is pinned to CLI
**v0.5.7**; a contract change should surface as a :class:`ParseError`, never a
silent mis-parse.

Grammar (verified):
    [colab] Creating session 'NAME'...
    [colab] Session READY.
    [NAME] ENDPOINT | Hardware: T4 | Variant: GPU | Status: IDLE      # `status`
    [NAME] ENDPOINT | Hardware: T4 | Variant: GPU                      # `sessions`
      Last Execution: FILE[ | Cell: N] at TIME                         # `status` 2nd line
    [colab] No active sessions found on server.
    [colab] Uploaded 'LOCAL' to 'REMOTE'
    [colab] Downloaded 'REMOTE' to 'LOCAL'
    [colab] Stopping session 'NAME'...
    [colab] Session terminated.
    [colab] Backend rejected accelerator 'A100'. ...                   # stderr, exit 1
"""

from __future__ import annotations

import re

from colabctl.errors import (
    AcceleratorUnavailableError,
    ParseError,
    QuotaExceededError,
    ScopeError,
    TooManyAssignmentsError,
)
from colabctl.models import Accelerator, SessionInfo, SessionStatus, Variant

#: CLI version this grammar was verified against.
PINNED_CLI_VERSION = "0.5.7"

_SESSION_LINE = re.compile(
    r"^\[(?P<name>[^\]]*)\]\s+(?P<endpoint>\S+)"
    r"\s+\|\s+Hardware:\s+(?P<hw>\S+)"
    r"\s+\|\s+Variant:\s+(?P<variant>\S+)"
    r"(?:\s+\|\s+Status:\s+(?P<status>.+?))?\s*$"
)
_LAST_EXEC = re.compile(r"^\s+Last Execution:\s+(?P<detail>.+?)\s*$")
_BUSY = re.compile(r"^BUSY\s*\((?P<running>.*)\)\s*$")
_CREATING = re.compile(r"^\[colab\]\s+Creating session '(?P<name>[^']*)'\.\.\.\s*$")
_VERSION = re.compile(r"^Version:\s*(?P<version>\S+)\s*$", re.MULTILINE)

_NO_SERVER_SESSIONS = "No active sessions found on server."
_NO_LOCAL_SESSIONS = "No active sessions."
_SESSION_READY = "Session READY."
_SESSION_TERMINATED = "Session terminated."


def _accelerator_from_label(label: str) -> Accelerator:
    """Reverse the CLI's hardware label (``CPU`` → NONE, else the enum value)."""
    if label.upper() == "CPU":
        return Accelerator.NONE
    try:
        return Accelerator(label.upper())
    except ValueError as exc:  # unknown hardware label = contract drift
        raise ParseError(f"Unknown hardware label from CLI: {label!r}") from exc


def _variant_from_label(label: str) -> Variant:
    try:
        return Variant(label.upper())
    except ValueError as exc:
        raise ParseError(f"Unknown variant from CLI: {label!r}") from exc


def parse_session_line(line: str) -> SessionInfo | None:
    """Parse one ``[name] endpoint | Hardware: .. | Variant: ..`` line.

    Returns ``None`` for any line that isn't a session line (messages, ``ls``
    entries, blanks), so callers can scan mixed output safely.
    """
    m = _SESSION_LINE.match(line)
    if not m:
        return None

    status = SessionStatus.UNKNOWN
    running: str | None = None
    raw_status = m.group("status")
    if raw_status is not None:
        raw_status = raw_status.strip()
        if raw_status == "IDLE":
            status = SessionStatus.IDLE
        elif (busy := _BUSY.match(raw_status)) is not None:
            status = SessionStatus.BUSY
            running = busy.group("running") or None
        else:
            status = SessionStatus.BUSY if raw_status.startswith("BUSY") else SessionStatus.UNKNOWN

    return SessionInfo(
        name=m.group("name"),
        endpoint=m.group("endpoint"),
        accelerator=_accelerator_from_label(m.group("hw")),
        variant=_variant_from_label(m.group("variant")),
        status=status,
        running=running,
    )


def parse_sessions_output(text: str) -> list[SessionInfo]:
    """Parse ``colab sessions`` output into the list of active sessions."""
    if _NO_SERVER_SESSIONS in text:
        return []
    return [s for line in text.splitlines() if (s := parse_session_line(line)) is not None]


def parse_status_output(text: str) -> list[SessionInfo]:
    """Parse ``colab status`` output, attaching the optional Last-Execution line."""
    if _NO_LOCAL_SESSIONS in text:
        return []
    sessions: list[SessionInfo] = []
    for line in text.splitlines():
        last_exec = _LAST_EXEC.match(line)
        if last_exec is not None and sessions:
            sessions[-1].last_execution = last_exec.group("detail")
            continue
        info = parse_session_line(line)
        if info is not None:
            sessions.append(info)
    return sessions


def parse_new_output(text: str) -> tuple[str | None, bool]:
    """Parse ``colab new`` output → (session_name_if_known, ready)."""
    name: str | None = None
    for line in text.splitlines():
        creating = _CREATING.match(line)
        if creating is not None:
            name = creating.group("name")
    return name, (_SESSION_READY in text)


def parse_version(text: str) -> str | None:
    """Extract the version from ``colab version`` output."""
    m = _VERSION.search(text)
    return m.group("version") if m else None


def parse_upload_ok(text: str) -> bool:
    return "Uploaded '" in text and "' to '" in text


def parse_download_ok(text: str) -> bool:
    return "Downloaded '" in text and "' to '" in text


def parse_terminated(text: str) -> bool:
    return _SESSION_TERMINATED in text


def parse_ls_output(text: str) -> list[str]:
    """Parse ``colab ls`` output (bare path lines; ``[colab] ...`` lines skipped)."""
    entries: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("[colab]"):
            continue
        entries.append(line)
    return entries


def raise_for_known_errors(*, stdout: str, stderr: str, returncode: int, argv: list[str]) -> None:
    """Translate recognizable CLI failures into the typed error taxonomy.

    Called by the adapter on a non-zero exit (and opportunistically on success,
    since the CLI sometimes prints actionable failures to stderr with exit 1).
    """
    blob = f"{stdout}\n{stderr}"

    if (m := re.search(r"rejected accelerator '(?P<acc>[^']+)'", blob)) is not None:
        raise AcceleratorUnavailableError(
            f"Colab backend rejected accelerator '{m.group('acc')}' "
            "(no quota/entitlement on this tier).",
            accelerator=m.group("acc"),
        )
    if "TooManyAssignments" in blob or "too many assignments" in blob.lower():
        raise TooManyAssignmentsError(blob.strip())
    if (
        "SCOPE_NOT_PERMITTED" in blob
        or "insufficient authentication scopes" in blob
        or ("colaboratory" in blob and "scope" in blob.lower())
    ):
        raise ScopeError(blob.strip())
    if "QUOTA_EXCEEDED" in blob or ("compute units" in blob.lower() and "exhaust" in blob.lower()):
        raise QuotaExceededError(blob.strip())
