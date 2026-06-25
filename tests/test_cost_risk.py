"""Spot-risk source (Phase 2c) — contract-tested against a captured AWS-feed shape."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from colabctl.cost.risk import SpotRisk, SpotRiskSource
from colabctl.models import Accelerator

_SAMPLE = json.dumps(
    {
        "ranges": [
            {"index": 0, "label": "<5%", "dots": 0, "max": 5},
            {"index": 1, "label": "5-10%", "dots": 1, "max": 11},
            {"index": 2, "label": "10-15%", "dots": 2, "max": 16},
            {"index": 3, "label": "15-20%", "dots": 3, "max": 22},
            {"index": 4, "label": ">20%", "dots": 4, "max": 100},
        ],
        "instance_types": {
            "p5.48xlarge": {"cores": 192, "ram_gb": 2048.0, "emr": False},
            "p5.4xlarge": {"cores": 16, "ram_gb": 256.0, "emr": False},
            "g4dn.xlarge": {"cores": 4, "ram_gb": 16.0, "emr": True},
            "g4dn.2xlarge": {"cores": 8, "ram_gb": 32.0, "emr": True},
            "m7i.large": {"cores": 2, "ram_gb": 8.0, "emr": True},  # CPU → skipped
        },
        "spot_advisor": {
            "us-east-1": {
                "Linux": {
                    "p5.48xlarge": {"s": 50, "r": 0},  # H100
                    "g4dn.xlarge": {"s": 70, "r": 4},  # T4
                    "m7i.large": {"s": 60, "r": 1},  # not a GPU we model
                }
            },
            "us-west-2": {
                "Linux": {
                    "p5.4xlarge": {"s": 40, "r": 0},  # H100
                    "g4dn.2xlarge": {"s": 66, "r": 4},  # T4
                }
            },
        },
    }
).encode()


async def test_aggregates_per_accelerator_and_resolves_labels(tmp_path: Path) -> None:
    rows = await SpotRiskSource(fetch=lambda _u: _ok(), home=tmp_path).risk()
    by = {r.accelerator: r for r in rows}

    assert set(by) == {Accelerator.H100, Accelerator.T4}  # CPU instance skipped
    h100 = by[Accelerator.H100]
    assert h100.interruption_range == 0 and h100.range_label == "<5%"
    assert h100.savings_pct == 45 and h100.samples == 2  # mean(50, 40)
    t4 = by[Accelerator.T4]
    assert t4.interruption_range == 4 and t4.range_label == ">20%"
    assert t4.savings_pct == 68  # mean(70, 66)
    # sorted safest-first
    assert rows[0].accelerator is Accelerator.H100


async def test_filter_by_accelerator(tmp_path: Path) -> None:
    rows = await SpotRiskSource(fetch=lambda _u: _ok(), home=tmp_path).risk(
        accelerator=Accelerator.T4
    )
    assert len(rows) == 1 and rows[0].accelerator is Accelerator.T4


async def test_falls_back_to_last_good_on_error(tmp_path: Path) -> None:
    state = {"ok": True}

    async def fetch(_url: str) -> bytes:
        if not state["ok"]:
            raise RuntimeError("down")
        return _SAMPLE

    src = SpotRiskSource(fetch=fetch, home=tmp_path, ttl=timedelta(0))
    first = await src.risk()
    state["ok"] = False
    second = await src.risk()  # fetch fails → last-good cache
    assert {r.accelerator for r in second} == {r.accelerator for r in first}


def test_acceptable_threshold() -> None:
    risk = SpotRisk(
        accelerator=Accelerator.A100,
        interruption_range=2,
        range_label="10-15%",
        savings_pct=50,
        samples=5,
    )
    assert risk.acceptable(2) and risk.acceptable(3)
    assert not risk.acceptable(1)  # bucket 2 exceeds a max of 1


async def _ok() -> bytes:
    return _SAMPLE
