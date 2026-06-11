"""ContentsTransfer over a faithful in-memory Jupyter contents/files API simulator.

The simulator implements the chunked-PUT contract (chunk 1 truncates, >1 appends, -1 is
the last) and ranged ``/files/`` GETs with ``Content-Range``, so the real
ContentsTransfer logic — chunking, range streaming, size verification, and the
no-range/no-route fallbacks — is exercised end to end without a network.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from colabctl.errors import FileTransferError
from colabctl.transport.native.client import ColabBackendClient
from colabctl.transport.native.contents import ContentsTransfer


class FakeContentsServer:
    """Minimal Jupyter contents + /files API over an in-memory filesystem.

    The proxy URL carries a tunnel path prefix (e.g. ``/tun/m/ep``), so the API
    markers appear mid-path — we split on them rather than assuming a leading slash.
    """

    def __init__(self, *, support_ranges: bool = True) -> None:
        self.fs: dict[str, bytes] = {}
        self.support_ranges = support_ranges
        self.put_chunks: list[int | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/files/" in path:
            return self._files(request, path.split("/files/", 1)[1])
        key = path.split("/api/contents/", 1)[1]
        if request.method == "PUT":
            return self._put(request, key)
        return self._get(request, key)

    def _put(self, request: httpx.Request, key: str) -> httpx.Response:
        model = json.loads(request.content)
        data = base64.b64decode(model["content"])
        chunk = model.get("chunk")
        self.put_chunks.append(chunk)
        if chunk in (None, 1):
            self.fs[key] = data
        else:  # 2.. or -1 → append
            self.fs[key] = self.fs.get(key, b"") + data
        return httpx.Response(201, json={"name": key, "path": key})

    def _get(self, request: httpx.Request, key: str) -> httpx.Response:
        if request.url.params.get("content") == "0":
            return httpx.Response(200, json={"size": len(self.fs.get(key, b""))})
        encoded = base64.b64encode(self.fs.get(key, b"")).decode()
        return httpx.Response(200, json={"format": "base64", "content": encoded})

    def _files(self, request: httpx.Request, key: str) -> httpx.Response:
        data = self.fs.get(key, b"")
        rng = request.headers.get("Range")
        if not self.support_ranges or rng is None:
            return httpx.Response(404 if key not in self.fs else 200, content=data)
        spec = rng.split("=", 1)[1]
        start_s, end_s = spec.split("-")
        start = int(start_s)
        end = min(int(end_s), len(data) - 1)
        chunk = data[start : end + 1]
        return httpx.Response(
            206,
            content=chunk,
            headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"},
        )


def _transfer(server: FakeContentsServer, *, chunk_size: int) -> ContentsTransfer:
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return ContentsTransfer(ColabBackendClient(http), chunk_size=chunk_size)


PROXY = "https://proxy.example/tun/m/ep"


# -- upload ------------------------------------------------------------------


async def test_single_chunk_upload_omits_chunk_field(tmp_path: Path) -> None:
    server = FakeContentsServer()
    transfer = _transfer(server, chunk_size=1024)
    src = tmp_path / "small.bin"
    src.write_bytes(b"hello world")
    await transfer.upload(PROXY, "tok", src, "content/small.bin")
    assert server.fs["content/small.bin"] == b"hello world"
    assert server.put_chunks == [None]  # single PUT, no chunk marker


async def test_chunked_upload_reassembles_exactly(tmp_path: Path) -> None:
    server = FakeContentsServer()
    transfer = _transfer(server, chunk_size=4)
    payload = bytes(range(256)) * 4  # 1024 bytes → 256 chunks of 4
    src = tmp_path / "big.bin"
    src.write_bytes(payload)
    await transfer.upload(PROXY, "tok", src, "content/big.bin")
    assert server.fs["content/big.bin"] == payload
    # First chunk is 1, last is -1, and there is exactly one of each.
    assert server.put_chunks[0] == 1
    assert server.put_chunks[-1] == -1
    assert server.put_chunks.count(-1) == 1


async def test_upload_reports_progress(tmp_path: Path) -> None:
    server = FakeContentsServer()
    transfer = _transfer(server, chunk_size=4)
    src = tmp_path / "p.bin"
    src.write_bytes(b"A" * 10)
    seen: list[tuple[int, int]] = []
    await transfer.upload(
        PROXY, "tok", src, "content/p.bin", on_progress=lambda d, t: seen.append((d, t))
    )
    assert seen[-1] == (10, 10)  # ends at full size
    assert all(t == 10 for _, t in seen)


async def test_upload_size_mismatch_raises(tmp_path: Path) -> None:
    class LyingServer(FakeContentsServer):
        def _get(self, request, key):
            if request.url.params.get("content") == "0":
                return httpx.Response(200, json={"size": 999})  # wrong
            return super()._get(request, key)

    server = LyingServer()
    transfer = _transfer(server, chunk_size=1024)
    src = tmp_path / "f.bin"
    src.write_bytes(b"data")
    with pytest.raises(FileTransferError, match="size mismatch"):
        await transfer.upload(PROXY, "tok", src, "content/f.bin")


async def test_upload_http_error_raises(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transfer = ContentsTransfer(ColabBackendClient(http))
    src = tmp_path / "f.bin"
    src.write_bytes(b"x")
    with pytest.raises(FileTransferError):
        await transfer.upload(PROXY, "tok", src, "content/f.bin")


# -- download ----------------------------------------------------------------


async def test_ranged_download_streams_whole_file(tmp_path: Path) -> None:
    server = FakeContentsServer(support_ranges=True)
    server.fs["content/r.bin"] = bytes(range(256)) * 10  # 2560 bytes
    transfer = _transfer(server, chunk_size=100)  # forces many ranged reads
    dest = tmp_path / "out.bin"
    await transfer.download(PROXY, "tok", "content/r.bin", dest)
    assert dest.read_bytes() == server.fs["content/r.bin"]


async def test_download_falls_back_to_contents_when_no_ranges(tmp_path: Path) -> None:
    server = FakeContentsServer(support_ranges=False)
    server.fs["content/r.bin"] = b"contents-fallback-bytes"
    transfer = _transfer(server, chunk_size=4)
    dest = tmp_path / "out.bin"
    await transfer.download(PROXY, "tok", "content/r.bin", dest)
    assert dest.read_bytes() == b"contents-fallback-bytes"


async def test_download_reports_progress(tmp_path: Path) -> None:
    server = FakeContentsServer(support_ranges=True)
    server.fs["content/r.bin"] = b"Z" * 250
    transfer = _transfer(server, chunk_size=100)
    seen: list[tuple[int, int]] = []
    dest = tmp_path / "out.bin"
    await transfer.download(
        PROXY, "tok", "content/r.bin", dest, on_progress=lambda d, t: seen.append((d, t))
    )
    assert seen[-1] == (250, 250)
