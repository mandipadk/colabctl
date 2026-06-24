"""Runtime-side Google Drive transfer — checkpoint straight from the VM (Pillar 3b).

Cuts the laptop out of the checkpoint loop. The old ``drive_checkpoint_hooks`` moved
every byte runtime → local tempfile → Drive (and back); for real ML state (GB of
weights) that double hop is impossible. Here the **runtime** talks to Drive directly:
the client injects a short-lived Drive token to a ``0600`` file on the VM, then runs
this helper there to resumable-upload (or ranged-download) between the VM's local disk
and the user's My Drive — no client memory or bandwidth in the path.

Everything is **pure-stdlib** (``urllib``) so it runs on a bare runtime and can be
exercised offline by running the emitted code against a mock Drive server. The Drive API
base URLs are parameters (default Google's) precisely so that test can point them at the
mock. Builders here emit the code; a kernel exec runs it and returns a framed JSON result.

**Quota project (important for ADC user credentials).** Per-user Google credentials must
name a *quota project* with the Drive API enabled, or Drive returns ``403`` (it tries to
bill the credential's origin project, where the API is disabled). Pass ``quota_project``
to send ``x-goog-user-project``; on the VM it must be a project the user owns with the
Drive API enabled. The helper returns the real HTTP error body so the cause is visible.
"""

from __future__ import annotations

import json
from typing import Any

#: Where the client injects the short-lived Drive token on the VM (``0600``).
DEFAULT_TOKEN_PATH = "/content/.colabctl/drive_token"
#: The My Drive folder durable artifacts are upserted into (owned by the user).
DEFAULT_FOLDER = "colabctl"
DEFAULT_API_BASE = "https://www.googleapis.com"
DEFAULT_UPLOAD_BASE = "https://www.googleapis.com"
_DEFAULT_CHUNK = 8 * 1024 * 1024  # 8 MiB resumable chunks

_F_BEGIN = "<<<COLABCTL_DRIVE>>>"
_F_END = "<<<COLABCTL_DRIVEEND>>>"
_TOKEN_SENTINEL = "COLABCTL_TOKEN_OK"


# --- the runtime helper (pure stdlib; runs on the VM) ------------------------

#: Defines ``upload(...)`` and ``download(...)`` against the Drive REST API using only
#: ``urllib`` — resumable upload (POST/PATCH init → ``Content-Range`` PUT chunks, 308 =
#: resume-incomplete) and ranged ``alt=media`` download. Authenticated JSON/media calls
#: carry the bearer + optional ``x-goog-user-project``; the resumable session PUTs are
#: pre-authorized by the session URI itself. On any HTTP error the call returns a result
#: with the real status + body so the failure is diagnosable. No third-party import.
DRIVE_HELPER_SOURCE = r"""
import hashlib, json, os, urllib.request, urllib.parse, urllib.error


def _token(token_path):
    with open(token_path) as f:
        return f.read().strip()


def _headers(token, qp, extra=None):
    h = {"Authorization": "Bearer " + token}
    if qp:
        h["x-goog-user-project"] = qp
    if extra:
        h.update(extra)
    return h


def _get_json(api_base, token, qp, q):
    params = urllib.parse.urlencode({"q": q, "fields": "files(id,name)"})
    req = urllib.request.Request(
        api_base + "/drive/v3/files?" + params, method="GET", headers=_headers(token, qp))
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode()).get("files", [])


def _ensure_folder(api_base, token, qp, name):
    q = "name='%s' and mimeType='application/vnd.google-apps.folder' and trashed=false" % name
    files = _get_json(api_base, token, qp, q)
    if files:
        return files[0]["id"]
    body = json.dumps({"name": name, "mimeType": "application/vnd.google-apps.folder"}).encode()
    req = urllib.request.Request(
        api_base + "/drive/v3/files", data=body, method="POST",
        headers=_headers(token, qp, {"Content-Type": "application/json"}))
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())["id"]


def _find(api_base, token, qp, name, folder_id):
    q = "name='%s' and '%s' in parents and trashed=false" % (name, folder_id)
    files = _get_json(api_base, token, qp, q)
    return files[0]["id"] if files else None


def _http_error(exc):
    body = ""
    try:
        body = exc.read().decode()[:500]
    except Exception:
        pass
    return {"ok": False, "error": "HTTP %d: %s" % (exc.code, exc.reason), "body": body}


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest()


def _file_md5(api_base, token, qp, file_id):
    req = urllib.request.Request(
        api_base + "/drive/v3/files/" + file_id + "?fields=md5Checksum",
        method="GET", headers=_headers(token, qp))
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode()).get("md5Checksum")


def _patch_meta(api_base, token, qp, file_id, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        api_base + "/drive/v3/files/" + file_id, data=data, method="PATCH",
        headers=_headers(token, qp, {"Content-Type": "application/json"}))
    with urllib.request.urlopen(req) as resp:
        resp.read()


def _raw_upload(local, name, folder_id, token, qp, upload_base, chunk):
    size = os.path.getsize(local)
    init = urllib.request.Request(
        upload_base + "/upload/drive/v3/files?uploadType=resumable",
        data=json.dumps({"name": name, "parents": [folder_id]}).encode(), method="POST",
        headers=_headers(token, qp, {"Content-Type": "application/json; charset=UTF-8",
                                     "X-Upload-Content-Length": str(size)}))
    with urllib.request.urlopen(init) as resp:
        location = resp.headers.get("Location")
    if not location:
        raise RuntimeError("Drive did not return a resumable session URI")
    file_id, sent = None, 0
    with open(local, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data and sent > 0:
                break
            end = sent + len(data) - 1
            crange = "bytes */0" if size == 0 else "bytes %d-%d/%d" % (sent, end, size)
            req = urllib.request.Request(
                location, data=data, method="PUT", headers={"Content-Range": crange})
            try:
                with urllib.request.urlopen(req) as resp:
                    file_id = (json.loads(resp.read().decode() or "{}").get("id") or file_id)
            except urllib.error.HTTPError as exc:
                if exc.code != 308:  # 308 = resume incomplete → keep going
                    raise
            sent += len(data)
            if size == 0:
                break
    return file_id


def upload(local, name, folder, token_path, api_base, upload_base, qp, chunk):
    # Crash-safe versioning: upload to a temp blob, verify its content end-to-end (Drive's
    # md5 vs the local file's), and ONLY then trash the old checkpoint and promote the temp
    # to the canonical name. So a crash mid-upload (the temp is incomplete) or a corrupt
    # upload (md5 mismatch) can never destroy the last-good checkpoint.
    try:
        token = _token(token_path)
        folder_id = _ensure_folder(api_base, token, qp, folder)
        tmp = name + ".colabctl-new"
        stale = _find(api_base, token, qp, tmp, folder_id)  # clean an interrupted prior run
        if stale:
            _patch_meta(api_base, token, qp, stale, {"trashed": True})
        new_id = _raw_upload(local, tmp, folder_id, token, qp, upload_base, chunk)
        local_md5 = _md5(local)
        remote_md5 = _file_md5(api_base, token, qp, new_id)
        if remote_md5 and remote_md5 != local_md5:
            _patch_meta(api_base, token, qp, new_id, {"trashed": True})
            return {"ok": False, "error": "checksum mismatch: local %s != drive %s" % (
                local_md5, remote_md5)}
        old_id = _find(api_base, token, qp, name, folder_id)
        if old_id and old_id != new_id:  # trash the prior good version, then promote the temp
            _patch_meta(api_base, token, qp, old_id, {"trashed": True})
        _patch_meta(api_base, token, qp, new_id, {"name": name})
        return {"ok": True, "id": new_id, "bytes": os.path.getsize(local), "md5": local_md5}
    except urllib.error.HTTPError as exc:
        return _http_error(exc)


def download(name, local, folder, token_path, api_base, qp, chunk):
    try:
        token = _token(token_path)
        folder_id = _ensure_folder(api_base, token, qp, folder)
        file_id = _find(api_base, token, qp, name, folder_id)
        if not file_id:  # recover a promotion interrupted after trashing old, before rename
            file_id = _find(api_base, token, qp, name + ".colabctl-new", folder_id)
        if not file_id:
            return {"ok": False, "error": "not found: " + name}
        url = api_base + "/drive/v3/files/" + file_id + "?alt=media"
        done = 0
        with open(local, "wb") as out:
            while True:
                rng = {"Range": "bytes=%d-%d" % (done, done + chunk - 1)}
                req = urllib.request.Request(url, method="GET", headers=_headers(token, qp, rng))
                try:
                    with urllib.request.urlopen(req) as resp:
                        data, code = resp.read(), resp.getcode()
                except urllib.error.HTTPError as exc:
                    if exc.code == 416:  # range past EOF → finished
                        break
                    raise
                if not data:
                    break
                out.write(data)
                done += len(data)
                if code == 200 or len(data) < chunk:  # whole file or last partial chunk
                    break
        return {"ok": True, "bytes": done}
    except urllib.error.HTTPError as exc:
        return _http_error(exc)
"""


# --- builders ----------------------------------------------------------------


def build_token_inject_code(token: str, *, token_path: str = DEFAULT_TOKEN_PATH) -> str:
    """Code that writes ``token`` to a ``0600`` file on the VM (transient; not logged)."""
    return (
        "import os, pathlib\n"
        f"_p = pathlib.Path({json.dumps(token_path)})\n"
        "_p.parent.mkdir(parents=True, exist_ok=True)\n"
        f"_p.write_text({json.dumps(token)})\n"
        "os.chmod(_p, 0o600)\n"
        f"print({json.dumps(_TOKEN_SENTINEL)})\n"
    )


def _framed_call(call_expr: str) -> str:
    return (
        DRIVE_HELPER_SOURCE
        + "\n_r = "
        + call_expr
        + "\n"
        + f"print({json.dumps(_F_BEGIN)} + json.dumps(_r) + {json.dumps(_F_END)})\n"
    )


def build_drive_upload_code(
    local_path: str,
    drive_name: str,
    *,
    folder: str = DEFAULT_FOLDER,
    token_path: str = DEFAULT_TOKEN_PATH,
    api_base: str = DEFAULT_API_BASE,
    upload_base: str = DEFAULT_UPLOAD_BASE,
    quota_project: str = "",
    chunk_size: int = _DEFAULT_CHUNK,
) -> str:
    """Code that resumable-uploads a VM file to Drive and prints a framed result."""
    args = ", ".join(
        json.dumps(x)
        for x in (local_path, drive_name, folder, token_path, api_base, upload_base, quota_project)
    )
    return _framed_call(f"upload({args}, {int(chunk_size)})")


def build_drive_download_code(
    drive_name: str,
    local_path: str,
    *,
    folder: str = DEFAULT_FOLDER,
    token_path: str = DEFAULT_TOKEN_PATH,
    api_base: str = DEFAULT_API_BASE,
    quota_project: str = "",
    chunk_size: int = _DEFAULT_CHUNK,
) -> str:
    """Code that ranged-downloads a Drive file to a VM path and prints a framed result."""
    args = ", ".join(
        json.dumps(x) for x in (drive_name, local_path, folder, token_path, api_base, quota_project)
    )
    return _framed_call(f"download({args}, {int(chunk_size)})")


# --- result parsing ----------------------------------------------------------


def token_inject_ok(text: str) -> bool:
    return _TOKEN_SENTINEL in text


def parse_drive_result(text: str) -> dict[str, Any]:
    """Extract the framed JSON result of an upload/download helper call."""
    start = text.find(_F_BEGIN)
    end = text.find(_F_END, start + len(_F_BEGIN)) if start != -1 else -1
    if start == -1 or end == -1:
        raise ValueError("drive result frame not found in kernel output.")
    payload: dict[str, Any] = json.loads(text[start + len(_F_BEGIN) : end].strip())
    return payload


__all__ = [
    "DEFAULT_API_BASE",
    "DEFAULT_FOLDER",
    "DEFAULT_TOKEN_PATH",
    "DEFAULT_UPLOAD_BASE",
    "DRIVE_HELPER_SOURCE",
    "build_drive_download_code",
    "build_drive_upload_code",
    "build_token_inject_code",
    "parse_drive_result",
    "token_inject_ok",
]
