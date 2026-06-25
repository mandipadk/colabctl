"""Detached-job experiment tracking: env threading + lineage capture (Phase 4.10.5, part 3)."""

from __future__ import annotations

import json
from pathlib import Path

from colabctl.backends.base import JobSpec, JobState
from colabctl.jobs.backend import DetachedColabBackend
from colabctl.jobs.codes import RUNNER_SOURCE, build_launch_code
from colabctl.state import StateStore
from colabctl.tracking import LINEAGE_BEGIN, LINEAGE_END
from conftest import LocalExecTransport


def test_build_launch_code_threads_env_into_meta_and_runner():
    code = build_launch_code("j1", script="print(1)", env={"WANDB_API_KEY": "k"}, root="/tmp/x")
    assert "WANDB_API_KEY" in code  # the env is serialized into meta.json
    assert "os.environ.update(meta" in RUNNER_SOURCE  # the runner injects it for pip + the child


def _backend(tmp_path: Path) -> tuple[DetachedColabBackend, StateStore]:
    store = StateStore(home=tmp_path / "home")
    backend = DetachedColabBackend(
        LocalExecTransport(), state=store, root=str(tmp_path / "jobs"), poll_interval=0.05
    )
    return backend, store


async def test_detached_track_stores_track_and_user_env(tmp_path: Path):
    backend, store = _backend(tmp_path)
    info = await backend.submit(JobSpec(code="print('hi')", track="wandb", env={"MY_VAR": "v"}))
    record = store.get_job(info.id)
    assert record is not None
    assert record.track == "wandb"  # re-applied on resume
    assert record.env == {"MY_VAR": "v"}  # user env persisted (NOT the resolved creds)


def test_capture_lineage_records_once_from_job_output(tmp_path: Path):
    # The full pip-install→run path needs a real runtime (the test venv has no pip), so unit-test
    # the capture step that result() runs: a tracking job's printed lineage frame → audit ledger.
    from colabctl.state import StoredJob

    backend, store = _backend(tmp_path)
    job = StoredJob(
        id="colab-x", session_name="s", backend="colab", state=JobState.SUCCEEDED, track="wandb"
    )
    store.put_job(job)
    payload = {"wandb_run_id": "r1", "wandb_run_url": "https://wandb.ai/x/y/runs/r1"}
    text = f"some logs\n{LINEAGE_BEGIN}{json.dumps(payload)}{LINEAGE_END}\nmore\n"

    backend._capture_lineage(job, text)
    backend._capture_lineage(job, text)  # idempotent — must not duplicate

    lineage = [e for e in store.list_audit(job_id="colab-x") if e.action == "lineage"]
    assert len(lineage) == 1
    detail = json.loads(lineage[0].detail or "{}")
    assert detail["track"] == "wandb" and detail["wandb_run_id"] == "r1"
