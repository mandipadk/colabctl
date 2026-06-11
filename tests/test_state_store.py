"""State-store tests: round-trips, atomicity, recovery, and concurrency.

All offline — no credentials, no network. Concurrency and crash paths are exercised
directly (threads + a patched ``os.replace``) because the store's whole job is to be
correct exactly when two ``colabctl`` processes or a crash hit it at once.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from colabctl.errors import StateError
from colabctl.models import Accelerator, Variant
from colabctl.state import (
    SCHEMA_VERSION,
    RecordState,
    StateStore,
    StoredJob,
    StoredSession,
    default_home,
)
from colabctl.state import store as store_mod


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(home=tmp_path / "home")


def _session(name: str = "sess", **kw: object) -> StoredSession:
    return StoredSession(
        name=name, notebook_id="11111111-1111-1111-1111-111111111111", endpoint=f"ep-{name}", **kw
    )  # type: ignore[arg-type]


# -- basics ------------------------------------------------------------------


def test_load_missing_returns_empty(store: StateStore) -> None:
    doc = store.load()
    assert doc.schema_version == SCHEMA_VERSION
    assert doc.sessions == {} and doc.jobs == {}
    assert not store.path.exists()  # pure read must not create the file


def test_session_round_trip(store: StateStore) -> None:
    store.put_session(_session("a", accelerator=Accelerator.A100, variant=Variant.GPU))
    got = store.get_session("a")
    assert got is not None
    assert got.endpoint == "ep-a"
    assert got.accelerator is Accelerator.A100
    assert got.variant is Variant.GPU
    assert got.state is RecordState.ACTIVE
    assert [s.name for s in store.list_sessions()] == ["a"]


def test_session_delete(store: StateStore) -> None:
    store.put_session(_session("a"))
    assert store.delete_session("a") is True
    assert store.delete_session("a") is False  # idempotent, reports absence
    assert store.get_session("a") is None


def test_job_round_trip(store: StateStore) -> None:
    store.put_job(StoredJob(id="j1", session_name="a", code="print(1)", resumable=True))
    got = store.get_job("j1")
    assert got is not None
    assert got.session_name == "a" and got.resumable is True
    assert got.incarnations == 1
    assert store.delete_job("j1") is True


def test_persists_across_instances(tmp_path: Path) -> None:
    home = tmp_path / "home"
    StateStore(home=home).put_session(_session("a"))
    # A second, independent store object == a second process reading the same file.
    assert StateStore(home=home).get_session("a") is not None


def test_written_file_is_valid_json_with_version(store: StateStore) -> None:
    store.put_session(_session("a"))
    data = json.loads(store.path.read_text())
    assert data["schema_version"] == SCHEMA_VERSION
    assert "a" in data["sessions"]


def test_home_dir_is_private(store: StateStore) -> None:
    store.put_session(_session("a"))
    mode = store.home.stat().st_mode & 0o777
    assert mode == 0o700


# -- env override ------------------------------------------------------------


def test_default_home_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("COLABCTL_HOME", str(tmp_path / "custom"))
    assert default_home() == tmp_path / "custom"
    monkeypatch.delenv("COLABCTL_HOME", raising=False)
    assert default_home() == Path.home() / ".colabctl"


# -- datetime fidelity -------------------------------------------------------


def test_proxy_token_expiry_helpers() -> None:
    now = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
    s = _session("a", proxy_token_expires_at=now + timedelta(seconds=300))
    assert s.proxy_token_seconds_remaining(now=now) == 300
    assert s.proxy_token_expired(now=now) is False
    assert s.proxy_token_expired(now=now, margin=600) is True
    # Unknown expiry is treated as expired (refresh rather than trust).
    assert _session("b").proxy_token_expired(now=now) is True


def test_datetime_round_trips_timezone_aware(store: StateStore) -> None:
    when = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
    store.put_session(_session("a", proxy_token_expires_at=when))
    got = store.get_session("a")
    assert got is not None and got.proxy_token_expires_at == when
    assert got.proxy_token_expires_at.tzinfo is not None


# -- recovery ----------------------------------------------------------------


def test_corrupt_document_is_quarantined_not_lost(store: StateStore) -> None:
    store.home.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ this is not valid json")
    doc = store.load()  # must not raise
    assert doc.sessions == {}
    corrupt = list(store.home.glob("state.json.corrupt-*"))
    assert len(corrupt) == 1
    assert corrupt[0].read_text() == "{ this is not valid json"


def test_newer_schema_version_raises(store: StateStore) -> None:
    store.home.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"schema_version": SCHEMA_VERSION + 1, "sessions": {}}))
    with pytest.raises(StateError, match="newer colabctl"):
        store.load()


# -- transactional safety ----------------------------------------------------


def test_failed_transaction_writes_nothing(store: StateStore) -> None:
    store.put_session(_session("a"))
    before = store.path.read_text()
    with pytest.raises(RuntimeError, match="boom"):
        with store.transaction() as doc:
            doc.sessions["b"] = _session("b")
            raise RuntimeError("boom")
    assert store.path.read_text() == before  # body raised → no partial write
    assert store.get_session("b") is None


def test_crash_during_replace_preserves_original(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.put_session(_session("a"))
    before = store.path.read_text()

    def boom(_src: object, _dst: object) -> None:
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(store_mod.os, "replace", boom)
    with pytest.raises(OSError, match="simulated crash"):
        store.put_session(_session("b"))

    assert store.path.read_text() == before  # original intact (atomicity)
    leftovers = list(store.home.glob(".state-*.tmp"))
    assert leftovers == []  # temp file cleaned up, no leak


def test_concurrent_transactions_have_no_lost_updates(store: StateStore) -> None:
    # Seed a counter; many threads each increment it under the transaction lock.
    store.put_job(StoredJob(id="counter", incarnations=0))
    threads, iters = 6, 25

    def worker() -> None:
        for _ in range(iters):
            with store.transaction() as doc:
                doc.jobs["counter"].incarnations += 1

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    final = store.get_job("counter")
    assert final is not None
    assert final.incarnations == threads * iters  # exclusive lock prevented races
