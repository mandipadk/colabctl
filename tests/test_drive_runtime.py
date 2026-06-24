"""Runtime-side Drive helper: builders/parsers + a real resumable round-trip.

The helper is pure-stdlib (urllib), so we validate the actual Drive resumable-upload and
ranged-download protocol by running the emitted code as a subprocess against a local
mock Drive server — no Google, but the real wire logic (init → Content-Range chunks →
308/200; ranged alt=media). The single-request and chunked paths are both exercised.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from colabctl.drive_runtime import (
    build_drive_download_code,
    build_drive_upload_code,
    build_token_inject_code,
    parse_drive_result,
    token_inject_ok,
)

# -- builders / parsers (pure) -----------------------------------------------


def test_builders_compile() -> None:
    for code in (
        build_token_inject_code("tok"),
        build_drive_upload_code("/v/f.bin", "f.bin"),
        build_drive_download_code("f.bin", "/v/f.bin"),
    ):
        compile(code, "drive", "exec")


def test_token_inject_sets_0600_and_sentinel() -> None:
    code = build_token_inject_code("secret-token", token_path="/p/tok")
    assert "0o600" in code and "secret-token" in code
    assert token_inject_ok("noise\nCOLABCTL_TOKEN_OK\n")
    assert not token_inject_ok("nope")


def test_parse_drive_result_round_trips() -> None:
    framed = (
        "x<<<COLABCTL_DRIVE>>>" + json.dumps({"ok": True, "id": "f1"}) + "<<<COLABCTL_DRIVEEND>>>y"
    )
    assert parse_drive_result(framed) == {"ok": True, "id": "f1"}


def test_parse_drive_result_missing_frame_raises() -> None:
    with pytest.raises(ValueError, match="frame not found"):
        parse_drive_result("no frame here")


# -- live resumable round-trip against a mock Drive ---------------------------


class _MockDrive(ThreadingHTTPServer):
    folder = "colabctl"

    def setup_state(self) -> None:
        self.files: dict[str, tuple[str, bytes]] = {}  # name -> (id, bytes)
        self.sessions: dict[str, dict] = {}
        self._counter = 0
        self.last_user_project: str | None = None
        self.forbid = False  # when True, list queries 403 (simulates the ADC quota gate)
        self.bad_md5: str | None = None  # when set, md5Checksum responses return this wrong hash

    def next_id(self) -> int:
        self._counter += 1
        return self._counter


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silence
        pass

    server: _MockDrive

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def _json(self, obj: dict, code: int = 200) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.server.last_user_project = self.headers.get("x-goog-user-project")
        u = urlsplit(self.path)
        if self.server.forbid and "/drive/v3/files" in u.path and "alt=media" not in u.query:
            self._json({"error": {"message": "Drive API not enabled for project"}}, code=403)
            return
        if u.path.startswith("/drive/v3/files/"):  # files/<id>: md5 fields or alt=media download
            fid = u.path.rsplit("/", 1)[1]
            data = next((b for (i, b) in self.server.files.values() if i == fid), b"")
            if "fields=md5Checksum" in u.query and "alt=media" not in u.query:
                self._json({"md5Checksum": self.server.bad_md5 or hashlib.md5(data).hexdigest()})
                return
            rng = self.headers.get("Range")
            if rng:
                m = re.match(r"bytes=(\d+)-(\d+)", rng)
                start, end = int(m.group(1)), int(m.group(2))
                if start >= len(data) and data:
                    self.send_response(416)
                    self.end_headers()
                    return
                chunk = data[start : end + 1]
                self.send_response(206)
                self.send_header(
                    "Content-Range", f"bytes {start}-{start + len(chunk) - 1}/{len(data)}"
                )
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        q = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
        if "vnd.google-apps.folder" in q:
            self._json({"files": [{"id": "folder1", "name": self.server.folder}]})
            return
        name = (re.search(r"name='([^']*)'", q) or [None, ""])[1]
        files = (
            [{"id": self.server.files[name][0], "name": name}] if name in self.server.files else []
        )
        self._json({"files": files})

    def _start_session(self, file_id: str, name: str) -> None:
        sid = f"s{self.server.next_id()}"
        self.server.sessions[sid] = {"id": file_id, "name": name, "buf": bytearray()}
        host = self.headers.get("Host")
        self.send_response(200)
        self.send_header("Location", f"http://{host}/session/{sid}")
        self.end_headers()

    def do_POST(self) -> None:
        u = urlsplit(self.path)
        body = self._body()
        if u.path == "/upload/drive/v3/files":  # resumable init (create)
            meta = json.loads(body or b"{}")
            self._start_session(f"f{self.server.next_id()}", meta["name"])
            return
        if u.path == "/drive/v3/files":  # folder create (unused; folder pre-exists)
            self._json({"id": "folder1"})
            return
        self.send_response(404)
        self.end_headers()

    def do_PATCH(self) -> None:
        u = urlsplit(self.path)
        body = self._body()
        fid = u.path.rsplit("/", 1)[1]
        if u.path.startswith("/upload/"):  # legacy resumable init (update existing)
            name = next((n for n, (i, _) in self.server.files.items() if i == fid), fid)
            self._start_session(fid, name)
            return
        # metadata update: trash or rename a file by id (the crash-safe promotion path)
        meta = json.loads(body or b"{}")
        cur = next((n for n, (i, _) in self.server.files.items() if i == fid), None)
        if cur is not None:
            i, b = self.server.files.pop(cur)
            if not meta.get("trashed"):  # rename: re-key under the new name (trash = drop it)
                self.server.files[meta.get("name", cur)] = (i, b)
        self._json({"id": fid})

    def do_PUT(self) -> None:
        sid = urlsplit(self.path).path.rsplit("/", 1)[1]
        sess = self.server.sessions[sid]
        sess["buf"].extend(self._body())
        cr = self.headers.get("Content-Range", "")
        total = 0 if cr.endswith("/0") else int(cr.rsplit("/", 1)[1])
        if len(sess["buf"]) >= total:
            self.server.files[sess["name"]] = (sess["id"], bytes(sess["buf"]))
            self._json({"id": sess["id"]})
        else:
            self.send_response(308)
            self.end_headers()


@pytest.fixture
def drive_server():
    server = _MockDrive(("127.0.0.1", 0), _Handler)
    server.setup_state()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _run(code: str) -> dict:
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return parse_drive_result(proc.stdout)


def test_resumable_upload_and_download_round_trip(drive_server, tmp_path: Path) -> None:
    server, base = drive_server
    token = tmp_path / "tok"
    token.write_text("fake-bearer")
    payload = bytes(range(256)) * 40  # 10 KB
    src = tmp_path / "weights.bin"
    src.write_bytes(payload)

    # Chunk size of 4 KiB forces multiple resumable PUTs (308s) for a 10 KB file.
    up = _run(
        build_drive_upload_code(
            str(src),
            "weights.bin",
            token_path=str(token),
            api_base=base,
            upload_base=base,
            chunk_size=4096,
        )
    )
    assert up["ok"] is True and up["bytes"] == len(payload)
    assert server.files["weights.bin"][1] == payload  # landed byte-for-byte

    dest = tmp_path / "restored.bin"
    down = _run(
        build_drive_download_code(
            "weights.bin", str(dest), token_path=str(token), api_base=base, chunk_size=4096
        )
    )
    assert down["ok"] is True
    assert dest.read_bytes() == payload  # ranged download reassembled exactly


def test_upload_versions_each_write_and_verifies(drive_server, tmp_path: Path) -> None:
    server, base = drive_server
    token = tmp_path / "tok"
    token.write_text("t")
    src = tmp_path / "f.bin"

    src.write_bytes(b"first version")
    up1 = _run(
        build_drive_upload_code(
            str(src), "f.bin", token_path=str(token), api_base=base, upload_base=base
        )
    )
    assert up1["ok"] and server.files["f.bin"][1] == b"first version"
    first_id = server.files["f.bin"][0]

    src.write_bytes(b"second version, longer")
    up2 = _run(
        build_drive_upload_code(
            str(src), "f.bin", token_path=str(token), api_base=base, upload_base=base
        )
    )
    assert up2["ok"]
    assert server.files["f.bin"][1] == b"second version, longer"  # content replaced
    assert server.files["f.bin"][0] != first_id  # a NEW verified blob, not an in-place overwrite
    assert "f.bin.colabctl-new" not in server.files  # the temp was promoted; no leftover
    assert up2["md5"] == hashlib.md5(b"second version, longer").hexdigest()  # end-to-end verified


def test_corrupt_upload_rejected_and_last_good_preserved(drive_server, tmp_path: Path) -> None:
    server, base = drive_server
    token = tmp_path / "tok"
    token.write_text("t")
    src = tmp_path / "f.bin"

    src.write_bytes(b"good checkpoint")
    _run(
        build_drive_upload_code(
            str(src), "f.bin", token_path=str(token), api_base=base, upload_base=base
        )
    )
    good = server.files["f.bin"]

    # Drive reports a wrong md5 for the next upload → the helper must reject it and NOT
    # promote, leaving the prior good checkpoint completely intact (crash-safe versioning).
    server.bad_md5 = "0" * 32
    src.write_bytes(b"corrupted")
    res = _run(
        build_drive_upload_code(
            str(src), "f.bin", token_path=str(token), api_base=base, upload_base=base
        )
    )
    assert res["ok"] is False and "checksum mismatch" in res["error"]
    assert server.files["f.bin"] == good  # last-good untouched
    assert "f.bin.colabctl-new" not in server.files  # the bad temp was trashed


def test_download_missing_file_reports_not_found(drive_server, tmp_path: Path) -> None:
    _server, base = drive_server
    token = tmp_path / "tok"
    token.write_text("t")
    result = _run(
        build_drive_download_code(
            "absent.bin", str(tmp_path / "out"), token_path=str(token), api_base=base
        )
    )
    assert result["ok"] is False and "not found" in result["error"]


def test_http_403_is_captured_with_body(drive_server, tmp_path: Path) -> None:
    server, base = drive_server
    server.forbid = True  # simulate the ADC quota / API-not-enabled 403
    token = tmp_path / "tok"
    token.write_text("t")
    src = tmp_path / "f.bin"
    src.write_bytes(b"data")
    result = _run(
        build_drive_upload_code(
            str(src), "f.bin", token_path=str(token), api_base=base, upload_base=base
        )
    )
    assert result["ok"] is False
    assert "403" in result["error"]  # surfaced, not a bare traceback
    assert "not enabled" in result["body"]  # the real reason is visible


def test_quota_project_header_is_sent(drive_server, tmp_path: Path) -> None:
    server, base = drive_server
    token = tmp_path / "tok"
    token.write_text("t")
    src = tmp_path / "f.bin"
    src.write_bytes(b"x")
    _run(
        build_drive_upload_code(
            str(src),
            "f.bin",
            token_path=str(token),
            api_base=base,
            upload_base=base,
            quota_project="my-gcp-project",
        )
    )
    assert server.last_user_project == "my-gcp-project"  # x-goog-user-project threaded through
