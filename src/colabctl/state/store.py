"""The on-disk state store: one JSON document, atomic writes, cross-process safe.

Design constraints (from the 1x→10x plan, Pillar 1):

* **Atomic** — writes go to a temp file in the same directory, are ``fsync``'d, then
  ``os.replace``'d into place, so a reader (or a crash) never sees a half-written
  document. A failed transaction body writes nothing at all.
* **Cross-process safe** — a read-modify-write transaction holds an exclusive advisory
  lock (POSIX ``flock``) for its whole duration, so two ``colabctl`` invocations can't
  clobber each other. ``flock`` is released automatically if the holder dies, which is
  why it's preferred over an ``O_EXCL`` lockfile that can go stale.
* **Recoverable** — an unparseable document is quarantined (never silently discarded)
  and a fresh one returned, so a corrupt file degrades to "lost index, reconcilable via
  ``gc``" rather than a bricked tool. A document from a *newer* schema raises instead —
  we must not downgrade-corrupt a file a future client owns.

The store deliberately holds **no credentials** — only the metadata in
:mod:`colabctl.state.models`. Proxy tokens live in the secret store, referenced by key.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from colabctl.errors import StateError
from colabctl.fsutil import FileLock as _FileLock
from colabctl.fsutil import atomic_write as _atomic_write
from colabctl.observability import get_logger
from colabctl.state.models import (
    SCHEMA_VERSION,
    StateDocument,
    StoredJob,
    StoredSession,
    utcnow,
)

_log = get_logger("state")


def default_home() -> Path:
    """Resolve the colabctl home directory (``$COLABCTL_HOME`` or ``~/.colabctl``)."""
    env = os.environ.get("COLABCTL_HOME")
    return Path(env).expanduser() if env else Path.home() / ".colabctl"


class StateStore:
    """Reads/writes ``~/.colabctl/state.json`` with atomic, lock-guarded transactions.

    Args:
        home: override the colabctl home dir (defaults to ``$COLABCTL_HOME`` /
            ``~/.colabctl``). Tests pass a ``tmp_path``.
        now: injectable clock (used for quarantine filenames); defaults to UTC now.
    """

    def __init__(
        self,
        *,
        home: Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._home = home or default_home()
        self._now = now or utcnow

    @property
    def home(self) -> Path:
        return self._home

    @property
    def path(self) -> Path:
        return self._home / "state.json"

    @property
    def lock_path(self) -> Path:
        return self._home / "state.json.lock"

    # -- reads --------------------------------------------------------------

    def load(self) -> StateDocument:
        """Return the current document (an empty one if the file does not exist)."""
        if not self.path.exists():
            return StateDocument()
        with _FileLock(self.lock_path, exclusive=False):
            raw = self.path.read_text(encoding="utf-8")
        return self._parse(raw)

    def get_session(self, name: str) -> StoredSession | None:
        return self.load().sessions.get(name)

    def list_sessions(self) -> list[StoredSession]:
        return list(self.load().sessions.values())

    def get_job(self, job_id: str) -> StoredJob | None:
        return self.load().jobs.get(job_id)

    def list_jobs(self) -> list[StoredJob]:
        return list(self.load().jobs.values())

    # -- writes (transactional) ---------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[StateDocument]:
        """Exclusive read-modify-write: lock, load, yield, atomically persist on success.

        The body runs while the lock is held, so keep it short. If the body raises,
        **nothing is written** — the persist happens only after a clean ``yield``.
        """
        self._home.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _FileLock(self.lock_path, exclusive=True):
            raw = self.path.read_text(encoding="utf-8") if self.path.exists() else None
            doc = self._parse(raw) if raw is not None else StateDocument()
            yield doc
            _atomic_write(self.path, doc.model_dump_json(indent=2))

    def put_session(self, session: StoredSession) -> None:
        with self.transaction() as doc:
            doc.sessions[session.name] = session

    def delete_session(self, name: str) -> bool:
        with self.transaction() as doc:
            return doc.sessions.pop(name, None) is not None

    def put_job(self, job: StoredJob) -> None:
        with self.transaction() as doc:
            doc.jobs[job.id] = job

    def delete_job(self, job_id: str) -> bool:
        with self.transaction() as doc:
            return doc.jobs.pop(job_id, None) is not None

    # -- internals ----------------------------------------------------------

    def _parse(self, raw: str) -> StateDocument:
        try:
            doc = StateDocument.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            self._quarantine(raw, reason=str(exc))
            return StateDocument()
        if doc.schema_version > SCHEMA_VERSION:
            raise StateError(
                f"{self.path} was written by a newer colabctl (schema "
                f"{doc.schema_version} > {SCHEMA_VERSION}); refusing to downgrade it. "
                "Upgrade colabctl, or move the file aside."
            )
        # (No <-version migrations exist yet; older docs validate forward unchanged.)
        return doc

    def _quarantine(self, raw: str, *, reason: str) -> None:
        """Move an unparseable document aside so it is never silently lost."""
        stamp = self._now().strftime("%Y%m%dT%H%M%S%f")
        dest = self._home / f"state.json.corrupt-{stamp}"
        try:
            dest.write_text(raw, encoding="utf-8")
        except OSError:  # pragma: no cover - best-effort forensics
            _log.exception("state: could not quarantine corrupt document to %s", dest)
        _log.error(
            "state: %s was unparseable (%s); quarantined to %s and starting fresh. "
            "Run `colabctl gc` to reconcile against live assignments.",
            self.path,
            reason,
            dest,
        )


__all__ = ["StateStore", "default_home"]
