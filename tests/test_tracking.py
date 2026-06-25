"""Experiment-tracking core: env resolution, autolog preambles, lineage (Phase 4.10.5)."""

from __future__ import annotations

import json

from colabctl.tracking import (
    LINEAGE_BEGIN,
    LINEAGE_END,
    parse_lineage,
    requirements_for,
    resolve_tracking_env,
    tracking_postamble,
    tracking_preamble,
)


def _store(**secrets: str):
    return lambda account: secrets.get(account)


def test_unknown_tracker_injects_nothing():
    assert resolve_tracking_env(None, "job-1", secret_get=_store()) == {}
    assert resolve_tracking_env("comet", "job-1", secret_get=_store()) == {}
    assert requirements_for(None) == [] and requirements_for("wandb") == ["wandb"]


def test_wandb_env_injects_creds_and_lineage_tags():
    env = resolve_tracking_env(
        "wandb", "colab-abc", secret_get=_store(**{"wandb:api_key": "wk"}), project="p", entity="e"
    )
    assert env["WANDB_API_KEY"] == "wk"
    assert env["WANDB_RUN_GROUP"] == "colab-abc"  # outbound lineage
    assert env["WANDB_TAGS"] == "colabctl,colabctl-job:colab-abc"
    assert env["WANDB_PROJECT"] == "p" and env["WANDB_ENTITY"] == "e"
    assert env["COLABCTL_JOB_ID"] == "colab-abc"
    assert "WANDB_MODE" not in env  # has a key → not disabled


def test_wandb_fail_open_disables_when_no_key():
    env = resolve_tracking_env("wandb", "j", secret_get=_store())
    assert env["WANDB_MODE"] == "disabled"  # runs the fn without W&B rather than blocking


def test_resolve_fails_open_when_secret_store_raises():
    def boom(_account: str) -> str | None:
        raise RuntimeError("no keyring backend available")  # headless CI / no OS keychain

    env = resolve_tracking_env("wandb", "j", secret_get=boom)
    assert env["WANDB_MODE"] == "disabled"  # a broken secret backend must not crash the run


def test_mlflow_env_and_token_xor_basic():
    env = resolve_tracking_env(
        "mlflow",
        "j",
        secret_get=_store(
            **{
                "mlflow:tracking_uri": "https://mlflow.example",
                "mlflow:tracking_token": "tok",
                "mlflow:tracking_username": "u",
                "mlflow:tracking_password": "pw",
            }
        ),
    )
    assert env["MLFLOW_TRACKING_URI"] == "https://mlflow.example"
    assert env["MLFLOW_TRACKING_TOKEN"] == "tok"
    # both token and basic present → basic dropped (token wins, avoids MLflow's override surprise)
    assert "MLFLOW_TRACKING_USERNAME" not in env and "MLFLOW_TRACKING_PASSWORD" not in env


def test_preambles_are_valid_python():
    for track in ("wandb", "mlflow"):
        compile(tracking_preamble(track), f"<{track}-pre>", "exec")
        compile(tracking_postamble(track), f"<{track}-post>", "exec")
    assert tracking_preamble(None) == "" and tracking_postamble("wandb") == ""
    assert "wandb.init" in tracking_preamble("wandb").replace("_cc_", "")
    assert "autolog" in tracking_preamble("mlflow")
    assert "last_active_run" in tracking_postamble("mlflow")


def test_parse_lineage_roundtrip():
    payload = {"wandb_run_id": "r1", "wandb_run_url": "https://wandb.ai/x/y/runs/r1"}
    text = f"some logs\n{LINEAGE_BEGIN}{json.dumps(payload)}{LINEAGE_END}\nmore logs\n"
    assert parse_lineage(text) == payload
    assert parse_lineage("no markers here") is None
    # takes the last frame if several were printed
    one = f"{LINEAGE_BEGIN}{json.dumps({'a': 1})}{LINEAGE_END}"
    two = one + f"{LINEAGE_BEGIN}{json.dumps({'b': 2})}{LINEAGE_END}"
    assert parse_lineage(two) == {"b": 2}
