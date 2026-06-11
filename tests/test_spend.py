"""CcuInfo parsing + the pre-allocation spend guard."""

from __future__ import annotations

from colabctl.models import Accelerator, CcuInfo
from colabctl.spend import spend_report

# The verified live ccu-info shape (canary, 2026-06-11).
_RAW = {
    "assignmentsCount": 1,
    "consumptionRateHourly": 1.96,
    "currentBalance": 100.0,
    "eligibleGpus": ["T4", "L4", "A100"],
    "eligibleTpus": ["V5E1"],
}


def test_ccu_info_parses_real_shape() -> None:
    ccu = CcuInfo.from_raw(_RAW)
    assert ccu is not None
    assert ccu.current_balance == 100.0
    assert ccu.consumption_rate_hourly == 1.96
    assert ccu.assignments_count == 1
    assert ccu.eligible_gpus == ["T4", "L4", "A100"]
    assert round(ccu.runway_hours, 1) == 51.0  # 100 / 1.96


def test_ccu_info_tolerates_extra_and_missing_fields() -> None:
    ccu = CcuInfo.from_raw({"currentBalance": 5.0, "somethingNew": 1})  # extra ignored
    assert ccu is not None and ccu.current_balance == 5.0
    assert ccu.consumption_rate_hourly is None and ccu.eligible_gpus == []
    assert ccu.runway_hours is None  # no rate → unknown


def test_ccu_info_from_non_dict_is_none() -> None:
    assert CcuInfo.from_raw(None) is None
    assert CcuInfo.from_raw("nope") is None


def test_spend_report_healthy_is_clean() -> None:
    blockers, warnings = spend_report(CcuInfo.from_raw(_RAW), [Accelerator.A100])
    assert blockers == [] and warnings == []


def test_spend_report_zero_balance_blocks() -> None:
    ccu = CcuInfo.from_raw({"currentBalance": 0.0})
    blockers, _ = spend_report(ccu, [Accelerator.T4])
    assert blockers and "balance" in blockers[0]


def test_spend_report_low_runway_warns() -> None:
    ccu = CcuInfo.from_raw({"currentBalance": 1.0, "consumptionRateHourly": 4.0})  # 0.25h
    blockers, warnings = spend_report(ccu, [Accelerator.T4])
    assert blockers == []
    assert any("balance left" in w for w in warnings)


def test_spend_report_ineligible_gpu_warns_not_blocks() -> None:
    ccu = CcuInfo.from_raw({"currentBalance": 50.0, "eligibleGpus": ["T4"]})
    blockers, warnings = spend_report(ccu, [Accelerator.H100])
    assert blockers == []  # the ladder/backend arbitrates entitlement — informational only
    assert any("entitled GPUs" in w for w in warnings)


def test_spend_report_none_ccu_is_noop() -> None:
    assert spend_report(None, [Accelerator.A100]) == ([], [])
