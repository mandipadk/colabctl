"""Contract tests for backend state/accelerator maps against real provider enums."""

from __future__ import annotations

import contextlib

import pytest

from colabctl.backends.base import JobState
from colabctl.backends.hf_backend import hf_flavor, hf_state
from colabctl.backends.kaggle_backend import kaggle_accelerator, kaggle_state
from colabctl.backends.modal_backend import modal_gpu
from colabctl.backends.runpod_backend import runpod_gpu, runpod_state
from colabctl.backends.vertex_backend import vertex_accelerator, vertex_state
from colabctl.errors import ConfigurationError
from colabctl.models import Accelerator

# --- Vertex (real google.cloud.aiplatform JobState enum names) --------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("JOB_STATE_QUEUED", JobState.PENDING),
        ("JOB_STATE_PENDING", JobState.PENDING),
        ("JOB_STATE_RUNNING", JobState.RUNNING),
        ("JOB_STATE_SUCCEEDED", JobState.SUCCEEDED),
        ("JOB_STATE_FAILED", JobState.FAILED),
        ("JOB_STATE_CANCELLING", JobState.CANCELLED),
        ("JOB_STATE_CANCELLED", JobState.CANCELLED),
        ("JOB_STATE_PARTIALLY_SUCCEEDED", JobState.SUCCEEDED),
        ("JOB_STATE_PAUSED", JobState.UNKNOWN),
        ("JOB_STATE_EXPIRED", JobState.UNKNOWN),
        ("", JobState.UNKNOWN),
        ("garbage", JobState.UNKNOWN),
    ],
)
def test_vertex_state(raw, expected):
    assert vertex_state(raw) is expected


# --- HF Jobs (real huggingface_hub JobStage values) -------------------------


@pytest.mark.parametrize(
    "stage,expected",
    [
        ("RUNNING", JobState.RUNNING),
        ("COMPLETED", JobState.SUCCEEDED),
        ("ERROR", JobState.FAILED),
        ("DELETED", JobState.CANCELLED),
        ("DELETING", JobState.CANCELLED),
        ("", JobState.PENDING),
    ],
)
def test_hf_state(stage, expected):
    assert hf_state(stage) is expected


# --- Kaggle (real KernelWorkerStatus values) --------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        ("QUEUED", JobState.PENDING),
        ("RUNNING", JobState.RUNNING),
        ("COMPLETE", JobState.SUCCEEDED),
        ("ERROR", JobState.FAILED),
        ("CANCEL_REQUESTED", JobState.CANCELLED),
        ("CANCEL_ACKNOWLEDGED", JobState.CANCELLED),
    ],
)
def test_kaggle_state(status, expected):
    assert kaggle_state(status) is expected


# --- RunPod (real pod desiredStatus values) ---------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        ("RUNNING", JobState.RUNNING),
        ("EXITED", JobState.SUCCEEDED),
        ("TERMINATED", JobState.CANCELLED),
        ("PAUSED", JobState.PENDING),
        ("", JobState.PENDING),
    ],
)
def test_runpod_state(status, expected):
    assert runpod_state(status) is expected


# --- accelerator maps: explicit contracts -----------------------------------


def test_accelerator_explicit_contracts():
    assert modal_gpu(Accelerator.NONE) is None
    assert modal_gpu(Accelerator.T4) == "T4"

    assert vertex_accelerator(Accelerator.NONE)[0] is None
    assert vertex_accelerator(Accelerator.T4) == ("NVIDIA_TESLA_T4", "n1-standard-4")

    assert hf_flavor(Accelerator.NONE) == "cpu-basic"
    assert hf_flavor(Accelerator.T4) == "t4-small"

    assert kaggle_accelerator(Accelerator.NONE) == (False, None)
    assert kaggle_accelerator(Accelerator.T4) == (True, "NvidiaTeslaT4")

    assert runpod_gpu(Accelerator.T4) == "NVIDIA T4"


def test_kaggle_rejects_non_t4_gpu():
    with pytest.raises(ConfigurationError):
        kaggle_accelerator(Accelerator.A100)


def test_runpod_rejects_cpu():
    with pytest.raises(ConfigurationError):
        runpod_gpu(Accelerator.NONE)


# --- robustness: every accelerator either maps or raises ConfigurationError --

_MAPPERS = [
    ("modal", lambda a: modal_gpu(a)),
    ("vertex", lambda a: vertex_accelerator(a)),
    ("hf", lambda a: hf_flavor(a)),
    ("kaggle", lambda a: kaggle_accelerator(a)),
    ("runpod", lambda a: runpod_gpu(a)),
]


@pytest.mark.parametrize("name,fn", _MAPPERS, ids=[m[0] for m in _MAPPERS])
@pytest.mark.parametrize("acc", list(Accelerator))
def test_accelerator_mapping_never_crashes(name, fn, acc):
    # Must never raise KeyError/TypeError — only ConfigurationError for unsupported.
    with contextlib.suppress(ConfigurationError):
        fn(acc)
