"""DriveCheckpointer orchestration: token injection + runtime-direct up/download + hooks.

A fake transport records the executed code and returns canned framed results, so the
orchestration (inject a fresh token, run the Drive helper on the VM, parse the result,
build lifecycle hooks) is verified without a runtime or Google.
"""

from __future__ import annotations

import json

import pytest

from colabctl.auth import StaticTokenProvider
from colabctl.drive import DriveCheckpointer
from colabctl.drive_runtime import token_inject_ok
from colabctl.errors import FileTransferError
from colabctl.models import ExecutionResult, StreamOutput
from conftest import FakeTransport

_DRIVE_BEGIN, _DRIVE_END = "<<<COLABCTL_DRIVE>>>", "<<<COLABCTL_DRIVEEND>>>"


class DriveScriptTransport(FakeTransport):
    """Records executed code; answers token-inject and drive-helper execs with canned output."""

    name = "drivescript"

    def __init__(self, *, upload_result: dict | None = None, download_result: dict | None = None):
        super().__init__()
        self.codes: list[str] = []
        self._upload = (
            upload_result if upload_result is not None else {"ok": True, "id": "f1", "bytes": 3}
        )
        self._download = (
            download_result if download_result is not None else {"ok": True, "bytes": 3}
        )

    async def execute(self, name, code, *, timeout=None, on_output=None) -> ExecutionResult:
        self.codes.append(code)
        if "COLABCTL_TOKEN_OK" in code:  # the token-inject builder prints this sentinel
            text = "COLABCTL_TOKEN_OK\n"
        elif "def download(" in code and "_r = download(" in code:
            text = _DRIVE_BEGIN + json.dumps(self._download) + _DRIVE_END
        else:
            text = _DRIVE_BEGIN + json.dumps(self._upload) + _DRIVE_END
        return ExecutionResult(status="ok", outputs=[StreamOutput(name="stdout", text=text)])


def _checkpointer() -> DriveCheckpointer:
    return DriveCheckpointer(StaticTokenProvider("bearer-xyz"))


async def test_checkpoint_injects_token_then_uploads() -> None:
    t = DriveScriptTransport(upload_result={"ok": True, "id": "fid", "bytes": 99})
    result = await _checkpointer().checkpoint_file(t, "sess", "/content/ckpt.bin", "ckpt.bin")
    assert result["id"] == "fid" and result["bytes"] == 99
    # First exec injected the token (with the secret), second ran the upload helper.
    assert token_inject_ok(t.codes[0]) and "bearer-xyz" in t.codes[0]
    assert "_r = upload(" in t.codes[1]


async def test_checkpoint_propagates_helper_failure() -> None:
    t = DriveScriptTransport(upload_result={"ok": False, "error": "quota exceeded"})
    with pytest.raises(FileTransferError, match="quota exceeded"):
        await _checkpointer().checkpoint_file(t, "sess", "/content/ckpt.bin", "ckpt.bin")


async def test_restore_tolerates_not_found() -> None:
    t = DriveScriptTransport(download_result={"ok": False, "error": "not found: ckpt.bin"})
    payload = await _checkpointer().restore_file(t, "sess", "ckpt.bin", "/content/ckpt.bin")
    assert payload["ok"] is False  # returned, not raised


async def test_token_inject_failure_raises() -> None:
    class NoSentinel(DriveScriptTransport):
        async def execute(self, name, code, *, timeout=None, on_output=None):
            return ExecutionResult(status="ok", outputs=[StreamOutput(name="stdout", text="")])

    with pytest.raises(FileTransferError, match="inject the Drive token"):
        await _checkpointer().checkpoint_file(NoSentinel(), "sess", "/c/x", "x")


async def test_hooks_checkpoint_and_restore_each_path() -> None:
    t = DriveScriptTransport()
    checkpoint, restore = _checkpointer().hooks(
        [("/content/a.bin", "a.bin"), ("/content/b.bin", "b.bin")]
    )
    await checkpoint(t, "sess")
    # 2 paths × (token inject + upload) = 4 execs.
    assert sum("_r = upload(" in c for c in t.codes) == 2
    t.codes.clear()
    await restore(t, "sess")
    assert sum("_r = download(" in c for c in t.codes) == 2
