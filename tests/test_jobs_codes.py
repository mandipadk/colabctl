"""Pure tests for the detached-job payloads: builders, parsers, and the real runner.

The runner is plain stdlib, so we run it for real (a local subprocess) and assert it
produces the on-disk contract the client reads — no Colab needed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from colabctl.errors import JobError
from colabctl.jobs.codes import (
    RUNNER_SOURCE,
    build_cancel_code,
    build_launch_code,
    build_poll_code,
    build_tail_code,
    parse_launch_pid,
    parse_status_frame,
    parse_tail_frame,
    remote_dir_for,
)

# -- builders ----------------------------------------------------------------


def test_remote_dir_for() -> None:
    assert remote_dir_for("abc", root="/r") == "/r/abc"


def test_runner_source_compiles() -> None:
    compile(RUNNER_SOURCE, "runner.py", "exec")  # must be valid Python


def test_launch_code_embeds_script_and_detaches() -> None:
    code = build_launch_code("job1", script="print('hi')", requirements=["torch"], root="/r")
    assert "start_new_session=True" in code  # detached process group
    assert "print('hi')" in code  # script embedded
    assert "/r/job1" in code
    assert "runner.py" in code and "meta.json" in code
    compile(code, "launch", "exec")


def test_poll_and_tail_and_cancel_compile() -> None:
    for code in (
        build_poll_code("j", root="/r"),
        build_tail_code("j", offset=10, max_bytes=100, root="/r"),
        build_cancel_code("j", root="/r"),
    ):
        compile(code, "c", "exec")


# -- parsers -----------------------------------------------------------------


def test_parse_launch_pid() -> None:
    assert parse_launch_pid("noise\n<<<COLABCTL_JOB>>>4242<<<COLABCTL_JOBEND>>>\n") == 4242


def test_parse_status_frame() -> None:
    text = '<<<COLABCTL_JOB>>>{"state": "running", "log_size": 5}<<<COLABCTL_JOBEND>>>'
    assert parse_status_frame(text) == {"state": "running", "log_size": 5}


def test_parse_tail_frame_round_trips_bytes() -> None:
    import base64

    payload = json.dumps({"offset": 11, "b64": base64.b64encode(b"hello bytes").decode()})
    data, offset = parse_tail_frame(f"<<<COLABCTL_JOB>>>{payload}<<<COLABCTL_JOBEND>>>")
    assert data == b"hello bytes"
    assert offset == 11


def test_missing_frame_raises() -> None:
    with pytest.raises(JobError):
        parse_status_frame("no markers here")


# -- the real runner (local subprocess) --------------------------------------


def _seed_job(d: Path, script: str, *, timeout=None, requirements=None) -> None:
    (d / "script.py").write_text(script)
    (d / "runner.py").write_text(RUNNER_SOURCE)
    (d / "meta.json").write_text(
        json.dumps({"requirements": requirements or [], "timeout": timeout})
    )


def test_runner_succeeds_and_writes_contract(tmp_path: Path) -> None:
    _seed_job(tmp_path, "print('hello from job')\n")
    subprocess.run([sys.executable, str(tmp_path / "runner.py")], check=True, cwd=tmp_path)
    assert (tmp_path / "exit_code").read_text().strip() == "0"
    assert json.loads((tmp_path / "status.json").read_text())["state"] == "succeeded"
    assert "hello from job" in (tmp_path / "log.txt").read_text()


def test_runner_records_failure(tmp_path: Path) -> None:
    _seed_job(tmp_path, "import sys\nsys.exit(7)\n")
    subprocess.run([sys.executable, str(tmp_path / "runner.py")], cwd=tmp_path)
    assert (tmp_path / "exit_code").read_text().strip() == "7"
    assert json.loads((tmp_path / "status.json").read_text())["state"] == "failed"


def test_runner_enforces_timeout(tmp_path: Path) -> None:
    _seed_job(tmp_path, "import time\ntime.sleep(30)\n", timeout=0.3)
    subprocess.run([sys.executable, str(tmp_path / "runner.py")], cwd=tmp_path, timeout=20)
    assert (tmp_path / "exit_code").read_text().strip() == "124"  # timeout sentinel
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "failed" and status.get("timed_out") is True
