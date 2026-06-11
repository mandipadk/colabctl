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

from colabctl import drive_runtime
from colabctl.auth.base import AuthProvider
from colabctl.errors import FileTransferError
from colabctl.observability import get_logger
from colabctl.transport.base import TransportAdapter

_log = get_logger("drive")

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


# --- runtime-direct checkpoints (Pillar 3b) ---------------------------------


class DriveCheckpointer:
    """Checkpoint/restore between a runtime's local disk and Drive — runtime-direct.

    Unlike :func:`drive_checkpoint_hooks` (which double-hops every byte through the
    client), this injects a short-lived Drive token to the VM and runs the transfer
    *there* (:mod:`colabctl.drive_runtime`), so checkpointing GB-scale weights never
    touches client memory or bandwidth. Built on the transport's ``execute`` only, so it
    works over any transport that runs Python on the runtime (native is the host).

    **Security:** the injected token is whatever ``auth`` yields. With ADC user
    credentials that token carries the full granted scope set (which includes
    ``drive.file`` — files the app created — but also the other Colab scopes); there is
    no clean way to mint a ``drive.file``-only access token from a broad user grant.
    The token is short-lived, re-injected each checkpoint, written ``0600``, and never
    logged. For strict least-privilege, pass an ``auth`` backed by a credential granted
    only ``drive.file``.
    """

    def __init__(
        self,
        auth: AuthProvider,
        *,
        folder: str = drive_runtime.DEFAULT_FOLDER,
        token_path: str = drive_runtime.DEFAULT_TOKEN_PATH,
        api_base: str = drive_runtime.DEFAULT_API_BASE,
        upload_base: str = drive_runtime.DEFAULT_UPLOAD_BASE,
        quota_project: str | None = None,
        chunk_size: int | None = None,
    ) -> None:
        self._auth = auth
        self._folder = folder
        self._token_path = token_path
        self._api_base = api_base
        self._upload_base = upload_base
        # Quota project (x-goog-user-project): ADC user credentials must name a project
        # with the Drive API enabled, or Drive returns 403. Required in practice for the
        # ADC path; None omits the header (fine for credentials that carry their own).
        self._quota_project = quota_project
        self._chunk_size = chunk_size

    async def _inject_token(self, transport: TransportAdapter, session: str) -> None:
        token = await self._auth.token()
        result = await transport.execute(
            session, drive_runtime.build_token_inject_code(token, token_path=self._token_path)
        )
        if not result.ok or not drive_runtime.token_inject_ok(result.text):
            raise FileTransferError("Failed to inject the Drive token onto the runtime.")

    def _resolved_quota_project(self) -> str:
        """The quota project to send: the explicit one, else the auth provider's (ADC)."""
        if self._quota_project is not None:
            return self._quota_project
        return getattr(self._auth, "quota_project_id", None) or ""

    def _upload_kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "folder": self._folder,
            "token_path": self._token_path,
            "api_base": self._api_base,
            "upload_base": self._upload_base,
            "quota_project": self._resolved_quota_project(),
        }
        if self._chunk_size is not None:
            kw["chunk_size"] = self._chunk_size
        return kw

    def _download_kwargs(self) -> dict[str, Any]:
        kw = self._upload_kwargs()
        kw.pop("upload_base")
        return kw

    @staticmethod
    def _detail(payload: dict[str, Any]) -> str:
        return f"{payload.get('error')} {payload.get('body', '')}".strip()

    async def checkpoint_file(
        self, transport: TransportAdapter, session: str, runtime_path: str, drive_name: str
    ) -> dict[str, Any]:
        """Upload ``runtime_path`` (on the VM) to Drive as ``drive_name``; return the result."""
        await self._inject_token(transport, session)
        code = drive_runtime.build_drive_upload_code(
            runtime_path, drive_name, **self._upload_kwargs()
        )
        result = await transport.execute(session, code)
        if not result.ok:
            raise FileTransferError(
                f"Drive checkpoint of {runtime_path} failed: {result.error or result.text[:200]}"
            )
        payload = drive_runtime.parse_drive_result(result.text)
        if not payload.get("ok"):
            raise FileTransferError(f"Drive checkpoint failed: {self._detail(payload)}")
        return payload

    async def restore_file(
        self, transport: TransportAdapter, session: str, drive_name: str, runtime_path: str
    ) -> dict[str, Any]:
        """Download ``drive_name`` from Drive to ``runtime_path`` on the VM; return the result.

        A missing Drive file is returned as ``{"ok": False, ...}`` (benign on a first run),
        not raised — only transport/exec failures raise.
        """
        await self._inject_token(transport, session)
        code = drive_runtime.build_drive_download_code(
            drive_name, runtime_path, **self._download_kwargs()
        )
        result = await transport.execute(session, code)
        if not result.ok:
            raise FileTransferError(
                f"Drive restore of {drive_name} failed: {result.error or result.text[:200]}"
            )
        return drive_runtime.parse_drive_result(result.text)

    def hooks(self, paths: list[tuple[str, str]]) -> tuple[LifecycleHook, LifecycleHook]:
        """Build (checkpoint, restore) lifecycle hooks for ``(runtime_path, drive_name)`` pairs.

        Restore tolerates a not-yet-existing Drive file (first run) by skipping it.
        """

        async def checkpoint(transport: TransportAdapter, session: str) -> None:
            for runtime_path, drive_name in paths:
                await self.checkpoint_file(transport, session, runtime_path, drive_name)

        async def restore(transport: TransportAdapter, session: str) -> None:
            for runtime_path, drive_name in paths:
                payload = await self.restore_file(transport, session, drive_name, runtime_path)
                if not payload.get("ok"):
                    _log.info("drive restore: %s not in Drive yet; skipping", drive_name)

        return checkpoint, restore
