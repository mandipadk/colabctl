"""Experiment-tracking integration: Weights & Biases + MLflow (Phase 4.10.5).

Opt-in via ``track="wandb"|"mlflow"`` on ``@remote`` / a detached job. The design is **pure
environment injection** — colabctl never imports wandb/mlflow itself; credentials come from the
secret store and travel only as environment variables (never baked into pickled code), and the
tracking library is installed + imported *on the runtime*. Two-way lineage:

* **outbound** — the run is tagged with ``COLABCTL_JOB_ID`` (via env vars the library honours at
  init: ``WANDB_RUN_GROUP``/``WANDB_TAGS`` for W&B, ``extra_tags`` for MLflow), so you can find
  the run from the job id.
* **inbound** — the on-runtime preamble prints a framed lineage line (run id + URL) that the
  client parses and records into the append-only audit ledger, so you can find the job from the
  run (durable — unlike anonymous/offline W&B runs which expire).

Verified against the 2026 docs: W&B env vars *override* ``wandb.init()`` kwargs (our injection
wins); the legacy ``wandb.integration.*.autolog`` LLM helpers were removed, so the durable
one-liner is ``wandb.init(sync_tensorboard=True)``; MLflow has no URL builder, so it's
hand-built from the tracking URI; for MLflow, basic-auth (user+pass) overrides the token.
"""

from __future__ import annotations

import json
from collections.abc import Callable

#: The only trackers we support (the plan's selective scope; ZenML/Comet/etc. are out).
TRACKERS: tuple[str, ...] = ("wandb", "mlflow")

#: secret-store account -> env var, per tracker. Accounts live under service "colabctl".
_SECRET_ENV: dict[str, dict[str, str]] = {
    "wandb": {
        "wandb:api_key": "WANDB_API_KEY",
        "wandb:base_url": "WANDB_BASE_URL",
    },
    "mlflow": {
        "mlflow:tracking_uri": "MLFLOW_TRACKING_URI",
        "mlflow:tracking_token": "MLFLOW_TRACKING_TOKEN",
        "mlflow:tracking_username": "MLFLOW_TRACKING_USERNAME",
        "mlflow:tracking_password": "MLFLOW_TRACKING_PASSWORD",
        "mlflow:experiment_name": "MLFLOW_EXPERIMENT_NAME",
    },
}

LINEAGE_BEGIN = "<<<COLABCTL_LINEAGE>>>"
LINEAGE_END = "<<<END_LINEAGE>>>"

SecretGet = Callable[[str], str | None]


def requirements_for(track: str | None) -> list[str]:
    """The pip requirement to add so the runtime can import the tracker."""
    return [track] if track in TRACKERS else []


def resolve_tracking_env(
    track: str | None,
    job_id: str,
    *,
    secret_get: SecretGet,
    project: str | None = None,
    entity: str | None = None,
) -> dict[str, str]:
    """Env vars to inject for ``track``, with creds pulled from the secret store.

    ``secret_get(account)`` returns the secret or None. Returns ``{}`` for an unknown/absent
    tracker. **Fail-open:** if no credential is found, W&B gets ``WANDB_MODE=disabled`` so the
    shipped function still runs end to end instead of blocking on a login prompt (MLflow has no
    disabled mode — it would fall back to an ephemeral local store, which the caller should warn
    about). Outbound lineage (group/tags) is injected as env so the shipped code needs no edit.
    """
    if track not in TRACKERS:
        return {}
    env: dict[str, str] = {"COLABCTL_JOB_ID": job_id}
    has_key = False
    for account, var in _SECRET_ENV[track].items():
        value = secret_get(account)
        if value:
            env[var] = value
            has_key = True
    if track == "wandb":
        env["WANDB_RUN_GROUP"] = job_id
        env["WANDB_TAGS"] = f"colabctl,colabctl-job:{job_id}"
        if project:
            env["WANDB_PROJECT"] = project
        if entity:
            env["WANDB_ENTITY"] = entity
        if not has_key:
            env["WANDB_MODE"] = "disabled"  # fail-open: run without W&B rather than block
    # token XOR basic — MLflow lets basic-auth override the token, so don't inject both.
    if (
        track == "mlflow"
        and env.get("MLFLOW_TRACKING_TOKEN")
        and env.get("MLFLOW_TRACKING_PASSWORD")
    ):
        env.pop("MLFLOW_TRACKING_USERNAME", None)
        env.pop("MLFLOW_TRACKING_PASSWORD", None)
    return env


def tracking_preamble(track: str | None) -> str:
    """On-runtime code to run **before** the user function (init/autolog + W&B lineage frame)."""
    if track == "wandb":
        return (
            "import os as _cc_os, json as _cc_json\n"
            "try:\n"
            "    import wandb as _cc_wandb\n"
            "    _cc_run = _cc_wandb.init(sync_tensorboard=True)\n"
            "    try:\n"
            "        _cc_run.config['colabctl_job_id'] = _cc_os.environ.get('COLABCTL_JOB_ID')\n"
            "    except Exception:\n"
            "        pass\n"
            "    print("
            + repr(LINEAGE_BEGIN)
            + " + _cc_json.dumps({'wandb_run_id': _cc_run.id, 'wandb_run_url': _cc_run.url,"
            " 'wandb_path': '/'.join(_cc_run.path)}) + " + repr(LINEAGE_END) + ", flush=True)\n"
            "except Exception:\n"
            "    pass\n"
        )
    if track == "mlflow":
        return (
            "try:\n"
            "    import os as _cc_os, mlflow as _cc_mlflow\n"
            "    _cc_mlflow.autolog("
            "extra_tags={'COLABCTL_JOB_ID': _cc_os.environ.get('COLABCTL_JOB_ID')})\n"
            "except Exception:\n"
            "    pass\n"
        )
    return ""


def tracking_postamble(track: str | None) -> str:
    """On-runtime code to run **after** the user function (MLflow run id/URL lineage frame)."""
    if track == "mlflow":
        return (
            "try:\n"
            "    import json as _cc_json, mlflow as _cc_mlflow\n"
            "    _cc_r = _cc_mlflow.last_active_run()\n"
            "    if _cc_r is not None:\n"
            "        _cc_rid = _cc_r.info.run_id\n"
            "        _cc_exp = _cc_r.info.experiment_id\n"
            "        _cc_url = _cc_mlflow.get_tracking_uri().rstrip('/') + '/#/experiments/' "
            "+ str(_cc_exp) + '/runs/' + str(_cc_rid)\n"
            "        print("
            + repr(LINEAGE_BEGIN)
            + " + _cc_json.dumps({'mlflow_run_id': _cc_rid, 'experiment_id': _cc_exp,"
            " 'mlflow_run_url': _cc_url}) + " + repr(LINEAGE_END) + ", flush=True)\n"
            "except Exception:\n"
            "    pass\n"
        )
    return ""


def parse_lineage(text: str) -> dict[str, object] | None:
    """Extract the last lineage frame the runtime printed (run id/URL), or None."""
    start = text.rfind(LINEAGE_BEGIN)
    if start == -1:
        return None
    end = text.find(LINEAGE_END, start)
    if end == -1:
        return None
    try:
        parsed = json.loads(text[start + len(LINEAGE_BEGIN) : end])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = [
    "LINEAGE_BEGIN",
    "LINEAGE_END",
    "TRACKERS",
    "parse_lineage",
    "requirements_for",
    "resolve_tracking_env",
    "tracking_postamble",
    "tracking_preamble",
]
