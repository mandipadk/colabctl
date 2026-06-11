"""Chunked file transfer over the runtime's Jupyter contents/files REST API (Pillar 3a).

Replaces the kernel-base64 path — capped near the websocket message limit (~10 MiB) and
carrying the whole payload as a code literal — with the proxy REST surface verified
header-only in Phase A §①.

* **Upload** uses the Jupyter contents *chunked-PUT* protocol (the exact contract
  JupyterLab uses): one PUT per chunk, ``chunk`` field = ``1, 2, …`` for all but the
  last and ``-1`` for the last (omitted entirely for a single-chunk file). Memory is
  bounded by the chunk size regardless of file size, and a final size check guards
  integrity.
* **Download** streams ranged GETs from the ``/files/`` handler when the proxy supports
  HTTP Range (bounded memory), and falls back to a single contents GET (base64) when it
  does not — so it is correct everywhere and streaming where possible.

The chunked-upload and ranged-download paths beyond a single request are exercised by
unit tests against a faithful API simulator and are flagged for live validation
(``spikes/phase_a_runtime.py transfer``); the single-PUT/GET paths are Phase-A-validated.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

import httpx

from colabctl.errors import FileTransferError
from colabctl.transport.native.client import ColabBackendClient

#: ``on_progress(bytes_done, total_bytes)`` — total may be 0 if unknown.
ProgressCallback = Callable[[int, int], None]

_DEFAULT_CHUNK = 4 * 1024 * 1024  # 4 MiB per request


def _ceil_div(n: int, d: int) -> int:
    return -(-n // d)


class ContentsTransfer:
    """Move files between the local machine and a runtime via its Jupyter proxy."""

    def __init__(self, client: ColabBackendClient, *, chunk_size: int = _DEFAULT_CHUNK) -> None:
        self._client = client
        self._chunk = chunk_size

    # -- upload -------------------------------------------------------------

    async def upload(
        self,
        proxy_url: str,
        proxy_token: str,
        local_path: Path,
        remote_path: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        size = local_path.stat().st_size
        path = remote_path.lstrip("/")
        nchunks = max(1, _ceil_div(size, self._chunk))
        sent = 0
        with local_path.open("rb") as f:
            for index in range(nchunks):
                data = f.read(self._chunk)
                body: dict[str, object] = {
                    "type": "file",
                    "format": "base64",
                    "content": base64.b64encode(data).decode(),
                }
                if nchunks > 1:
                    body["chunk"] = -1 if index == nchunks - 1 else index + 1
                resp = await self._client.proxy_request(
                    "PUT",
                    proxy_url,
                    f"/api/contents/{path}",
                    proxy_token=proxy_token,
                    json_body=body,
                )
                if resp.status_code not in (200, 201):
                    raise FileTransferError(
                        f"upload chunk {index + 1}/{nchunks} of {remote_path} failed: "
                        f"HTTP {resp.status_code} {resp.text[:200]!r}"
                    )
                sent += len(data)
                if on_progress is not None:
                    on_progress(sent, size)
        await self._verify_size(proxy_url, proxy_token, path, expected=size)

    async def _verify_size(
        self, proxy_url: str, proxy_token: str, path: str, *, expected: int
    ) -> None:
        """Best-effort integrity check: the server's reported file size matches what we sent."""
        resp = await self._client.proxy_request(
            "GET",
            proxy_url,
            f"/api/contents/{path}",
            proxy_token=proxy_token,
            params={"content": "0"},
        )
        if resp.status_code != 200:
            return  # the PUTs already succeeded; metadata is a bonus, not a gate
        try:
            actual = resp.json().get("size")
        except (ValueError, AttributeError):
            return
        if isinstance(actual, int) and actual != expected:
            raise FileTransferError(
                f"upload of {path} size mismatch: sent {expected} bytes, server has {actual}."
            )

    # -- download -----------------------------------------------------------

    async def download(
        self,
        proxy_url: str,
        proxy_token: str,
        remote_path: str,
        local_path: Path,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        path = remote_path.lstrip("/")
        if await self._download_ranged(proxy_url, proxy_token, path, local_path, on_progress):
            return
        # Fallback: a single contents GET returning the file as base64.
        resp = await self._client.proxy_request(
            "GET",
            proxy_url,
            f"/api/contents/{path}",
            proxy_token=proxy_token,
            params={"content": "1", "type": "file", "format": "base64"},
        )
        if resp.status_code != 200:
            raise FileTransferError(
                f"download of {remote_path} failed: HTTP {resp.status_code} {resp.text[:200]!r}"
            )
        try:
            content = resp.json().get("content", "")
        except ValueError as exc:
            raise FileTransferError(f"download of {remote_path}: response was not JSON.") from exc
        data = base64.b64decode(content)
        local_path.write_bytes(data)
        if on_progress is not None:
            on_progress(len(data), len(data))

    async def _download_ranged(
        self,
        proxy_url: str,
        proxy_token: str,
        path: str,
        local_path: Path,
        on_progress: ProgressCallback | None,
    ) -> bool:
        """Stream the file via ranged ``/files/`` GETs; return False to signal fallback."""
        first = await self._client.proxy_request(
            "GET",
            proxy_url,
            f"/files/{path}",
            proxy_token=proxy_token,
            headers={"Range": f"bytes=0-{self._chunk - 1}"},
        )
        if first.status_code == 200:  # no Range support, but the whole body came back
            local_path.write_bytes(first.content)
            if on_progress is not None:
                on_progress(len(first.content), len(first.content))
            return True
        if first.status_code != 206:  # route absent / not-found → let the caller fall back
            return False
        total = self._content_range_total(first)
        with local_path.open("wb") as out:
            out.write(first.content)
            done = len(first.content)
            if on_progress is not None and total is not None:
                on_progress(done, total)
            while total is not None and done < total:
                end = min(done + self._chunk, total) - 1
                resp = await self._client.proxy_request(
                    "GET",
                    proxy_url,
                    f"/files/{path}",
                    proxy_token=proxy_token,
                    headers={"Range": f"bytes={done}-{end}"},
                )
                if resp.status_code not in (200, 206):
                    raise FileTransferError(
                        f"ranged download of {path} failed at byte {done}: HTTP {resp.status_code}"
                    )
                if not resp.content:
                    break
                out.write(resp.content)
                done += len(resp.content)
                if on_progress is not None and total is not None:
                    on_progress(done, total)
        return True

    @staticmethod
    def _content_range_total(resp: httpx.Response) -> int | None:
        # e.g. "Content-Range: bytes 0-1048575/5242880" → 5242880
        header = resp.headers.get("Content-Range", "")
        if "/" in header:
            tail = header.rsplit("/", 1)[1].strip()
            if tail.isdigit():
                return int(tail)
        return None


__all__ = ["ContentsTransfer", "ProgressCallback"]
