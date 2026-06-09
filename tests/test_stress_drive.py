"""Adversarial tests for DriveSync query escaping (injection guard) + round-trips.

The fake in test_drive.py matches names with a naive ``name='([^']*)'`` regex, so it
can't validate filenames containing quotes/backslashes. This fake instead *correctly
un-escapes* the query literal — the inverse of ``drive._escape`` — so a round-trip here
proves the production escaping is sound end-to-end.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from colabctl.drive import DriveSync, _escape

_FOLDER_MIME = "application/vnd.google-apps.folder"


# --- direct escaping contract -----------------------------------------------


def test_escape_quote_and_backslash():
    assert _escape("a'b") == "a\\'b"
    assert _escape("a\\b") == "a\\\\b"
    # backslash escaped BEFORE quote, so a literal \' becomes \\\'
    assert _escape("a\\'b") == "a\\\\\\'b"
    assert _escape("plain") == "plain"


# --- a query parser that correctly reverses _escape -------------------------


def _read_literal(q: str, field: str) -> str | None:
    marker = f"{field}='"
    idx = q.find(marker)
    if idx == -1:
        return None
    i = idx + len(marker)
    out: list[str] = []
    while i < len(q):
        ch = q[i]
        if ch == "\\" and i + 1 < len(q):
            out.append(q[i + 1])
            i += 2
            continue
        if ch == "'":
            return "".join(out)
        out.append(ch)
        i += 1
    return "".join(out)


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


class CorrectFakeDrive:
    """Drive v3 fake that parses query literals with the inverse of _escape."""

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
        q = q or ""
        name = _read_literal(q, "name")  # un-escaped, inverse of drive._escape
        # Parent ids are our own generated tokens (no quotes/backslashes), so a
        # plain regex is exact here.
        pm = re.search(r"'([^']*)' in parents", q)
        parent = pm.group(1) if pm else None
        want_folder = _FOLDER_MIME in q
        out = []
        for fid, f in self._files.items():
            is_folder = f.get("mimeType") == _FOLDER_MIME
            if want_folder != is_folder:
                continue
            if name is not None and f["name"] != name:
                continue
            if parent is not None and f.get("parent") != parent:
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
    return DriveSync(service=CorrectFakeDrive())


# --- round-trips with adversarial names -------------------------------------

_ADVERSARIAL_NAMES = [
    "simple.pkl",
    "with space.txt",
    "quote's.bin",
    "back\\slash.dat",
    "both\\'mix.x",
    "ünïcödé_文件.ckpt",
    "tab\tand\nnewline",
    "many''''quotes",
]


@pytest.mark.parametrize("name", _ADVERSARIAL_NAMES)
async def test_put_get_roundtrip_adversarial_name(name):
    d = _drive()
    payload = name.encode() + b"\x00\xff"
    await d.put_bytes(name, payload)
    assert await d.get_bytes(name) == payload
    assert await d.exists(name) is True


async def test_special_names_do_not_collide():
    d = _drive()
    await d.put_bytes("a'b", b"one")
    await d.put_bytes("a\\b", b"two")
    assert await d.get_bytes("a'b") == b"one"
    assert await d.get_bytes("a\\b") == b"two"


async def test_upsert_overwrites_same_special_name():
    d = _drive()
    fid1 = await d.put_bytes("quote's.bin", b"v1")
    fid2 = await d.put_bytes("quote's.bin", b"v2")
    assert fid1 == fid2  # upsert, not duplicate
    assert await d.get_bytes("quote's.bin") == b"v2"
    assert await d.list_names() == ["quote's.bin"]


async def test_delete_special_name():
    d = _drive()
    await d.put_bytes("a'b\\c", b"x")
    assert await d.delete("a'b\\c") is True
    assert await d.get_bytes("a'b\\c") is None
    assert await d.delete("a'b\\c") is False  # idempotent


@given(name=st.text(min_size=1, max_size=40), data=st.binary(max_size=64))
async def test_roundtrip_property(name, data):
    d = _drive()
    await d.put_bytes(name, data)
    assert await d.get_bytes(name) == data
