"""Runtime-side payloads for detached jobs — the kernel as a control plane.

Pillar 2's core idea (validated by Phase A §③: the kernel survives a dropped
websocket): instead of running user code as a foreground cell that monopolizes the
kernel for hours, we write the code to the VM and launch it as a **detached,
supervised process**. The kernel is then free, so short execs can poll status, tail
the log by byte offset, and cancel — and a dropped connection costs a reconnect, not
the job.

Everything here is **pure**: builders that emit Python source for the kernel to run,
a self-contained ``runner.py`` template, and frame parsers. No transport, no network —
golden-tested offline. The job's source of truth lives on the VM's disk under
``<remote_dir>/`` so it outlives any client process:

    meta.json     spec echo (requirements, timeout, created_at)
    script.py     the user code
    runner.py     the supervisor (this module's RUNNER_SOURCE)
    pid           the detached process-group leader's pid
    status.json   {"state": "...", "started_at": ..., "finished_at": ...}
    log.txt       combined stdout+stderr, appended live
    exit_code     written last — its presence means "finished"
"""

from __future__ import annotations

import base64
import json
from typing import Any

from colabctl.errors import JobError

#: Default base directory for job state on the Colab VM (``/content`` is the writable
#: working dir; the dot-prefixed subdir keeps it out of the user's way).
DEFAULT_JOBS_ROOT = "/content/.colabctl/jobs"

# Frame markers (mirror the kernel file-transfer framing so output parsing is uniform).
_F_BEGIN = "<<<COLABCTL_JOB>>>"
_F_END = "<<<COLABCTL_JOBEND>>>"


def remote_dir_for(job_id: str, *, root: str = DEFAULT_JOBS_ROOT) -> str:
    """The on-VM directory holding one job's state."""
    return f"{root}/{job_id}"


# --- the runner (runs on the Colab VM, Python 3.x stdlib only) ---------------

#: Source of the supervisor written to ``<remote_dir>/runner.py`` and executed
#: detached. It installs requirements (logged), runs ``script.py`` as a child in its
#: own process group, tees output to ``log.txt``, and writes ``status.json``/``exit_code``
#: atomically so the client can read a consistent state at any instant. Pure stdlib so
#: it runs on a bare runtime; no colabctl import on the VM.
RUNNER_SOURCE = r"""
import json, os, subprocess, sys, time, signal

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "log.txt")
STATUS = os.path.join(HERE, "status.json")
EXIT = os.path.join(HERE, "exit_code")
META = os.path.join(HERE, "meta.json")
SCRIPT = os.path.join(HERE, "script.py")


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _status(state, **extra):
    doc = {"state": state, "pid": os.getpid()}
    doc.update(extra)
    _atomic_write(STATUS, json.dumps(doc))


def main():
    with open(META) as f:
        meta = json.load(f)
    started = time.time()
    _status("running", started_at=started)
    log = open(LOG, "a", buffering=1)

    def emit(msg):
        log.write(msg)
        log.flush()

    reqs = meta.get("requirements") or []
    if reqs:
        emit("[colabctl] installing %d requirement(s)...\n" % len(reqs))
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", *reqs],
            stdout=log, stderr=subprocess.STDOUT,
        )
        if pip.returncode != 0:
            emit("[colabctl] pip install failed (exit %d)\n" % pip.returncode)
            _status("failed", started_at=started, finished_at=time.time())
            _atomic_write(EXIT, str(pip.returncode))
            return

    timeout = meta.get("timeout")
    proc = subprocess.Popen(
        [sys.executable, "-u", SCRIPT],
        stdout=log, stderr=subprocess.STDOUT, cwd=HERE,
    )
    _status("running", started_at=started, child_pid=proc.pid)
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        emit("\n[colabctl] timeout after %ss; terminating\n" % timeout)
        proc.kill()
        proc.wait()
        _status("failed", started_at=started, finished_at=time.time(), timed_out=True)
        _atomic_write(EXIT, "124")
        return
    state = "succeeded" if code == 0 else "failed"
    _status(state, started_at=started, finished_at=time.time())
    _atomic_write(EXIT, str(code))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # never leave the job without a terminal state
        try:
            with open(LOG, "a") as f:
                f.write("\n[colabctl] runner crashed: %r\n" % exc)
        except Exception:
            pass
        _status("failed", crashed=True)
        _atomic_write(EXIT, "1")
"""


# --- builders (emit code the kernel executes) --------------------------------


def _frame_print(expr: str) -> str:
    """A print that wraps ``expr`` (a str expression) in the job markers."""
    return f"print({json.dumps(_F_BEGIN)} + ({expr}) + {json.dumps(_F_END)})"


def build_launch_code(
    job_id: str,
    *,
    script: str,
    requirements: list[str] | None = None,
    timeout: float | None = None,
    root: str = DEFAULT_JOBS_ROOT,
    created_at: float | None = None,
) -> str:
    """Code that writes the job files and spawns the runner detached; prints the pid.

    The runner is started with ``start_new_session=True`` (``setsid``), so it survives
    the kernel and gets its own process group — which is what makes whole-job cancel
    (``killpg``) and "the connection is not the data plane" work.
    """
    rdir = remote_dir_for(job_id, root=root)
    meta: dict[str, Any] = {
        "job_id": job_id,
        "requirements": list(requirements or []),
        "timeout": timeout,
        "created_at": created_at,
    }
    return (
        "import os, json, subprocess, sys\n"
        f"_d = {json.dumps(rdir)}\n"
        "os.makedirs(_d, exist_ok=True)\n"
        f"open(os.path.join(_d, 'script.py'), 'w').write({json.dumps(script)})\n"
        f"open(os.path.join(_d, 'runner.py'), 'w').write({json.dumps(RUNNER_SOURCE)})\n"
        f"open(os.path.join(_d, 'meta.json'), 'w').write({json.dumps(json.dumps(meta))})\n"
        "open(os.path.join(_d, 'log.txt'), 'a').close()\n"
        "_p = subprocess.Popen([sys.executable, os.path.join(_d, 'runner.py')],\n"
        "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        "    stdin=subprocess.DEVNULL, start_new_session=True, cwd=_d)\n"
        "open(os.path.join(_d, 'pid'), 'w').write(str(_p.pid))\n"
        f"{_frame_print('str(_p.pid)')}\n"
    )


def build_poll_code(job_id: str, *, root: str = DEFAULT_JOBS_ROOT) -> str:
    """Code that prints a framed JSON snapshot of the job's state (+ exit_code/log size)."""
    rdir = remote_dir_for(job_id, root=root)
    return (
        "import os, json\n"
        f"_d = {json.dumps(rdir)}\n"
        "_s = os.path.join(_d, 'status.json'); _e = os.path.join(_d, 'exit_code')\n"
        "_l = os.path.join(_d, 'log.txt')\n"
        "_doc = {}\n"
        "if os.path.exists(_s):\n"
        "    try: _doc = json.load(open(_s))\n"
        "    except Exception: _doc = {'state': 'unknown'}\n"
        "else:\n"
        "    _doc = {'state': 'missing'}\n"
        "if os.path.exists(_e):\n"
        "    _doc['exit_code'] = int(open(_e).read().strip() or '-1')\n"
        # Liveness: a 'running' status with no exit_code whose runner pid is gone means the
        # runner was killed (OOM/SIGKILL) without writing a terminal state — flag it so the
        # client resolves the job to FAILED instead of lying RUNNING forever.
        "_pid = _doc.get('pid')\n"
        "if _doc.get('state') == 'running' and 'exit_code' not in _doc and isinstance(_pid, int):\n"
        "    try:\n"
        "        os.kill(_pid, 0); _doc['runner_alive'] = True\n"
        "    except ProcessLookupError:\n"
        "        _doc['runner_alive'] = False\n"
        "    except OSError:\n"
        "        _doc['runner_alive'] = True\n"
        "_doc['log_size'] = os.path.getsize(_l) if os.path.exists(_l) else 0\n"
        f"{_frame_print('json.dumps(_doc)')}\n"
    )


def build_tail_code(
    job_id: str, *, offset: int = 0, max_bytes: int = 65536, root: str = DEFAULT_JOBS_ROOT
) -> str:
    """Code that prints a framed base64 slice of ``log.txt`` from ``offset`` (+ new offset).

    Reading by byte offset is what lets ``--follow`` resume exactly after any disconnect
    or process restart — the client persists the offset, not the bytes.
    """
    rdir = remote_dir_for(job_id, root=root)
    return (
        "import os, json, base64\n"
        f"_l = os.path.join({json.dumps(rdir)}, 'log.txt')\n"
        f"_off = {int(offset)}\n"
        "_data = b''\n"
        "if os.path.exists(_l):\n"
        "    with open(_l, 'rb') as _f:\n"
        "        _f.seek(_off)\n"
        f"        _data = _f.read({int(max_bytes)})\n"
        "_payload = json.dumps({'offset': _off + len(_data),"
        " 'b64': base64.b64encode(_data).decode()})\n"
        f"{_frame_print('_payload')}\n"
    )


def build_cancel_code(job_id: str, *, root: str = DEFAULT_JOBS_ROOT) -> str:
    """Code that signals the job's process group (SIGTERM→SIGKILL) and marks it cancelled."""
    rdir = remote_dir_for(job_id, root=root)
    return (
        "import os, json, signal, time\n"
        f"_d = {json.dumps(rdir)}\n"
        "_pidf = os.path.join(_d, 'pid'); _ok = False\n"
        "if os.path.exists(_pidf):\n"
        "    _pid = int(open(_pidf).read().strip() or '0')\n"
        "    if _pid > 0:\n"
        "        try:\n"
        "            os.killpg(os.getpgid(_pid), signal.SIGTERM); _ok = True\n"
        "            time.sleep(1)\n"
        "            try: os.killpg(os.getpgid(_pid), signal.SIGKILL)\n"
        "            except (ProcessLookupError, PermissionError): pass\n"
        "        except (ProcessLookupError, PermissionError): _ok = False\n"
        "_sp = os.path.join(_d, 'status.json')\n"
        "try:\n"
        "    _doc = json.load(open(_sp)) if os.path.exists(_sp) else {}\n"
        "except Exception: _doc = {}\n"
        "_doc['state'] = 'cancelled'\n"
        "open(_sp + '.tmp', 'w').write(json.dumps(_doc)); os.replace(_sp + '.tmp', _sp)\n"
        "open(os.path.join(_d, 'exit_code'), 'w').write('143')\n"
        "_payload = json.dumps({'cancelled': bool(_ok)})\n"
        f"{_frame_print('_payload')}\n"
    )


# --- frame parsers (pure) ----------------------------------------------------


def _extract_frame(text: str) -> str:
    start = text.find(_F_BEGIN)
    end = text.find(_F_END, start + len(_F_BEGIN)) if start != -1 else -1
    if start == -1 or end == -1:
        raise JobError("job control frame not found in kernel output.")
    return text[start + len(_F_BEGIN) : end].strip()


def parse_launch_pid(text: str) -> int:
    """The runner pid from a launch frame."""
    try:
        return int(_extract_frame(text))
    except ValueError as exc:
        raise JobError(f"could not parse launch pid: {_extract_frame(text)!r}") from exc


def parse_status_frame(text: str) -> dict[str, Any]:
    """The status dict from a poll frame."""
    try:
        doc: dict[str, Any] = json.loads(_extract_frame(text))
    except ValueError as exc:
        raise JobError("could not parse status frame as JSON.") from exc
    return doc


def parse_tail_frame(text: str) -> tuple[bytes, int]:
    """``(new_bytes, new_offset)`` from a tail frame."""
    doc = json.loads(_extract_frame(text))
    return base64.b64decode(doc.get("b64", "")), int(doc.get("offset", 0))


__all__ = [
    "DEFAULT_JOBS_ROOT",
    "RUNNER_SOURCE",
    "build_cancel_code",
    "build_launch_code",
    "build_poll_code",
    "build_tail_code",
    "parse_launch_pid",
    "parse_status_frame",
    "parse_tail_frame",
    "remote_dir_for",
]
