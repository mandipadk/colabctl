"""Tests for DriveSync + drive_checkpoint_hooks against a fake Drive v3 service."""

from __future__ import annotations

import re

from colabctl.drive import DriveSync, drive_checkpoint_hooks
from conftest import FakeTransport

_FOLDER_MIME = "application/vnd.google-apps.folder"


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _Files:
    def __init__(self, svc):
        self._svc = svc

    def list(self, q=None, spaces=None, fields=None):
        return _Exec(lambda: self._svc._list(q))

    def create(self, body=None, media_body=None, fields=None):
        return _Exec(lambda: self._svc._create(body, media_body))

    def update(self, fileId=None, media_body=None):
        return _Exec(lambda: self._svc._update(fileId, media_body))

    def get_media(self, fileId=None):
        return _Exec(lambda: self._svc._get_media(fileId))

    def delete(self, fileId=None):
        return _Exec(lambda: self._svc._delete(fileId))


class FakeDriveService:
    """Implements just enough of the Drive v3 query/upload semantics we use."""

    def __init__(self):
        self._files: dict[str, dict] = {}
        self._n = 0

    def files(self):
        return _Files(self)

    def _new_id(self) -> str:
        self._n += 1
        return f"id{self._n}"

    @staticmethod
    def _bytes(media) -> bytes:
        return b"" if media is None else bytes(media.getbytes(0, media.size()))

    def _list(self, q):
        out = []
        name_m = re.search(r"name='([^']*)'", q or "")
        parent_m = re.search(r"'([^']*)' in parents", q or "")
        want_folder = _FOLDER_MIME in (q or "")
        for fid, f in self._files.items():
            is_folder = f.get("mimeType") == _FOLDER_MIME
            if want_folder and not is_folder:
                continue
            if not want_folder and is_folder:
                continue
            if name_m and f["name"] != name_m.group(1):
                continue
            if parent_m and f.get("parent") != parent_m.group(1):
                continue
            out.append({"id": fid, "name": f["name"]})
        return {"files": out}

    def _create(self, body, media):
        fid = self._new_id()
        self._files[fid] = {
            "name": body["name"],
            "parent": (body.get("parents") or [None])[0],
            "mimeType": body.get("mimeType"),
            "data": self._bytes(media),
        }
        return {"id": fid, "name": body["name"]}

    def _update(self, fid, media):
        self._files[fid]["data"] = self._bytes(media)
        return {"id": fid}

    def _get_media(self, fid):
        return self._files[fid]["data"]

    def _delete(self, fid):
        self._files.pop(fid, None)
        return {}


def _drive() -> DriveSync:
    return DriveSync(service=FakeDriveService())


async def test_put_then_get_roundtrips():
    drive = _drive()
    await drive.put_bytes("model.pt", b"weights-123")
    assert await drive.get_bytes("model.pt") == b"weights-123"


async def test_put_upserts_by_name():
    drive = _drive()
    await drive.put_bytes("ckpt.bin", b"v1")
    await drive.put_bytes("ckpt.bin", b"v2")
    assert await drive.get_bytes("ckpt.bin") == b"v2"
    assert await drive.list_names() == ["ckpt.bin"]  # not duplicated


async def test_get_missing_returns_none():
    assert await _drive().get_bytes("nope") is None


async def test_exists_and_delete():
    drive = _drive()
    await drive.put_bytes("a.txt", b"x")
    assert await drive.exists("a.txt")
    assert await drive.delete("a.txt")
    assert not await drive.exists("a.txt")
    assert not await drive.delete("a.txt")  # idempotent


async def test_put_file_and_get_file(tmp_path):
    drive = _drive()
    src = tmp_path / "data.csv"
    src.write_bytes(b"col\n1\n")
    await drive.put_file(src)
    dest = tmp_path / "out.csv"
    assert await drive.get_file("data.csv", dest)
    assert dest.read_bytes() == b"col\n1\n"


async def test_checkpoint_and_restore_hooks(tmp_path):
    drive = _drive()
    transport = FakeTransport()
    checkpoint, restore = drive_checkpoint_hooks(drive, [("content/state.pkl", "state.pkl")])

    # Checkpoint: pull from the runtime (FakeTransport.download writes "downloaded") → Drive.
    await checkpoint(transport, "sess")
    assert await drive.get_bytes("state.pkl") == b"downloaded"

    # Restore: pull from Drive → push to the runtime.
    await restore(transport, "sess")
    assert transport.uploaded  # an upload happened during restore
    assert transport.uploaded[-1][2] == "content/state.pkl"
