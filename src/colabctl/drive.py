"""Durable file sync via the Google Drive API (user-OAuth).

Per Phase 0 §5 / the risk register: durable artifacts go to the human's **My Drive**
via **user-OAuth** (the ADC ``drive.file`` scope), never a service account (which
can't own Google-native files). Files live under a single app folder; ``put`` upserts
by name. This is the durable layer the lifecycle manager checkpoints/restores through.

``googleapiclient`` / ``google-auth`` are imported lazily (the ``drive`` extra).
Downloads use the simple ``get_media().execute()`` path — fine for notebooks/checkpoints;
chunked transfer for very large blobs is a future enhancement.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from colabctl.transport.base import TransportAdapter

DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive.file",)
_FOLDER_MIME = "application/vnd.google-apps.folder"
_DEFAULT_FOLDER = "colabctl"


def _escape(value: str) -> str:
    """Escape a value for a Drive query string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveSync:
    """Upsert/fetch files in a single Drive app-folder, owned by the user.

    Args:
        folder_name: the My Drive folder to store files under.
        credentials: a ``google.auth`` credentials object; defaults to ADC.
        service: an injected Drive v3 service (for tests).
    """

    def __init__(
        self,
        *,
        folder_name: str = _DEFAULT_FOLDER,
        credentials: Any | None = None,
        service: Any | None = None,
    ) -> None:
        self._folder_name = folder_name
        self._credentials = credentials
        self._service = service
        self._folder_id: str | None = None

    # -- async API ----------------------------------------------------------

    async def put_bytes(self, name: str, data: bytes, *, mimetype: str | None = None) -> str:
        return await asyncio.to_thread(self._put_sync, name, data, mimetype)

    async def put_file(self, local_path: str | Path, name: str | None = None) -> str:
        path = Path(local_path)
        return await self.put_bytes(name or path.name, path.read_bytes())

    async def get_bytes(self, name: str) -> bytes | None:
        return await asyncio.to_thread(self._get_sync, name)

    async def get_file(self, name: str, local_path: str | Path) -> bool:
        data = await self.get_bytes(name)
        if data is None:
            return False
        Path(local_path).write_bytes(data)
        return True

    async def exists(self, name: str) -> bool:
        return (await asyncio.to_thread(self._find_sync, name)) is not None

    async def list_names(self) -> list[str]:
        return await asyncio.to_thread(self._list_names_sync)

    async def delete(self, name: str) -> bool:
        return await asyncio.to_thread(self._delete_sync, name)

    # -- sync internals (run in a worker thread) ----------------------------

    def _svc(self) -> Any:
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self) -> Any:
        from googleapiclient.discovery import build

        creds = self._credentials
        if creds is None:
            import google.auth

            creds, _ = google.auth.default(scopes=list(DRIVE_SCOPES))
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def _media(self, data: bytes, mimetype: str | None) -> Any:
        from googleapiclient.http import MediaInMemoryUpload

        return MediaInMemoryUpload(
            data, mimetype=mimetype or "application/octet-stream", resumable=False
        )

    def _ensure_folder(self) -> str:
        if self._folder_id is not None:
            return self._folder_id
        svc = self._svc()
        query = (
            f"name='{_escape(self._folder_name)}' and mimeType='{_FOLDER_MIME}' and trashed=false"
        )
        found = svc.files().list(q=query, spaces="drive", fields="files(id,name)").execute()
        files = found.get("files", [])
        if files:
            self._folder_id = files[0]["id"]
        else:
            created = (
                svc.files()
                .create(body={"name": self._folder_name, "mimeType": _FOLDER_MIME}, fields="id")
                .execute()
            )
            self._folder_id = created["id"]
        return self._folder_id

    def _find_sync(self, name: str) -> str | None:
        svc = self._svc()
        folder = self._ensure_folder()
        query = f"name='{_escape(name)}' and '{folder}' in parents and trashed=false"
        found = svc.files().list(q=query, spaces="drive", fields="files(id,name)").execute()
        files = found.get("files", [])
        return files[0]["id"] if files else None

    def _put_sync(self, name: str, data: bytes, mimetype: str | None) -> str:
        svc = self._svc()
        folder = self._ensure_folder()
        media = self._media(data, mimetype)
        existing = self._find_sync(name)
        if existing is not None:
            svc.files().update(fileId=existing, media_body=media).execute()
            return existing
        created = (
            svc.files()
            .create(body={"name": name, "parents": [folder]}, media_body=media, fields="id")
            .execute()
        )
        return str(created["id"])

    def _get_sync(self, name: str) -> bytes | None:
        svc = self._svc()
        file_id = self._find_sync(name)
        if file_id is None:
            return None
        return bytes(svc.files().get_media(fileId=file_id).execute())

    def _list_names_sync(self) -> list[str]:
        svc = self._svc()
        folder = self._ensure_folder()
        found = (
            svc.files()
            .list(
                q=f"'{folder}' in parents and trashed=false",
                spaces="drive",
                fields="files(id,name)",
            )
            .execute()
        )
        return [f["name"] for f in found.get("files", [])]

    def _delete_sync(self, name: str) -> bool:
        svc = self._svc()
        file_id = self._find_sync(name)
        if file_id is None:
            return False
        svc.files().delete(fileId=file_id).execute()
        return True


# --- lifecycle integration --------------------------------------------------

LifecycleHook = Callable[[TransportAdapter, str], Awaitable[None]]


def drive_checkpoint_hooks(
    drive: DriveSync, paths: list[tuple[str, str]]
) -> tuple[LifecycleHook, LifecycleHook]:
    """Build (checkpoint, restore) hooks that sync runtime files through Drive.

    ``paths`` is a list of ``(remote_path_on_runtime, drive_name)`` pairs. Checkpoint
    pulls each runtime file and uploads it to Drive; restore pulls each from Drive and
    pushes it back onto a (re-assigned) runtime. Plug these into
    :class:`~colabctl.lifecycle.RuntimeLifecycleManager`.
    """

    async def checkpoint(transport: TransportAdapter, session: str) -> None:
        for remote, drive_name in paths:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                local = Path(tmp.name)
            try:
                await transport.download(session, remote, local)
                await drive.put_file(local, drive_name)
            finally:
                with contextlib.suppress(OSError):
                    local.unlink()

    async def restore(transport: TransportAdapter, session: str) -> None:
        for remote, drive_name in paths:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                local = Path(tmp.name)
            try:
                if await drive.get_file(drive_name, local):
                    await transport.upload(session, local, remote)
            finally:
                with contextlib.suppress(OSError):
                    local.unlink()

    return checkpoint, restore
