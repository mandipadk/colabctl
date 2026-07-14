"""Structural fingerprinting / drift detection (pure)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from colabctl.drift import (
    compare_structural_snapshot,
    skeleton_diff,
    structural_fingerprint,
    structural_skeleton,
    structural_snapshot,
)


def test_skeleton_discards_values_keeps_types() -> None:
    assert structural_skeleton({"token": "abc", "n": 3}) == {"n": "int", "token": "str"}
    assert structural_skeleton([{"id": "x"}]) == [{"id": "str"}]
    assert structural_skeleton([]) == []


def test_fingerprint_stable_across_values() -> None:
    a = {"endpoint": "gpu-1", "runtimeProxyInfo": {"token": "t1", "tokenExpiresInSeconds": 600}}
    b = {"endpoint": "gpu-2", "runtimeProxyInfo": {"token": "t2", "tokenExpiresInSeconds": 540}}
    assert structural_fingerprint(a) == structural_fingerprint(b)  # same shape, different values


def test_fingerprint_changes_on_added_key() -> None:
    base = {"endpoint": "x", "accelerator": "T4"}
    drifted = {"endpoint": "x", "accelerator": "T4", "region": "us"}
    assert structural_fingerprint(base) != structural_fingerprint(drifted)


def test_fingerprint_changes_on_type_change() -> None:
    assert structural_fingerprint({"variant": "GPU"}) != structural_fingerprint({"variant": 1})


def test_snapshot_fingerprints_raw_values_without_double_normalizing() -> None:
    integer_fingerprints, _ = structural_snapshot({"assignments": {"variant": 1}})
    string_fingerprints, _ = structural_snapshot({"assignments": {"variant": "1"}})
    assert integer_fingerprints["assignments"] != string_fingerprints["assignments"]


def test_snapshot_comparison_reports_drift() -> None:
    baseline_fingerprints, baseline_skeletons = structural_snapshot({"value": {"kind": 1}})
    actual_fingerprints, actual_skeletons = structural_snapshot({"value": {"kind": "1"}})
    healthy, notes = compare_structural_snapshot(
        actual_fingerprints,
        actual_skeletons,
        {"fingerprints": baseline_fingerprints, "skeletons": baseline_skeletons},
    )
    assert healthy is False
    assert notes[0].startswith("value DRIFTED:")


def test_snapshot_comparison_fails_on_missing_key() -> None:
    baseline_fingerprints, baseline_skeletons = structural_snapshot(
        {"assignments": {"count": 1}, "ccu-info": {"balance": 1.0}}
    )
    actual_fingerprints, actual_skeletons = structural_snapshot({"assignments": {"count": 1}})
    healthy, notes = compare_structural_snapshot(
        actual_fingerprints,
        actual_skeletons,
        {"fingerprints": baseline_fingerprints, "skeletons": baseline_skeletons},
    )
    assert healthy is False
    assert notes == ["ccu-info missing from captured responses"]


def test_snapshot_comparison_fails_without_baseline() -> None:
    fingerprints, skeletons = structural_snapshot({"assignments": {"count": 1}})
    assert compare_structural_snapshot(fingerprints, skeletons, None) == (
        False,
        ["required baseline is missing"],
    )


def test_key_order_does_not_matter() -> None:
    assert structural_fingerprint({"a": 1, "b": 2}) == structural_fingerprint({"b": 2, "a": 1})


def test_diff_reports_added_removed_and_changed() -> None:
    old = structural_skeleton({"endpoint": "x", "variant": "GPU", "old": 1})
    new = structural_skeleton({"endpoint": "x", "variant": 1, "new": "y"})
    diffs = set(skeleton_diff(old, new))
    assert "-old" in diffs  # removed
    assert "+new" in diffs  # added
    assert any(d.startswith("~variant") for d in diffs)  # type changed str→int


def test_diff_descends_into_nested_and_lists() -> None:
    old = structural_skeleton({"assignments": [{"endpoint": "x", "token": "t"}]})
    new = structural_skeleton({"assignments": [{"endpoint": "x"}]})
    diffs = skeleton_diff(old, new)
    assert diffs == ["-assignments.[].token"]


def test_no_diff_for_identical_shapes() -> None:
    skel = structural_skeleton({"a": [1], "b": {"c": "s"}})
    assert skeleton_diff(skel, skel) == []


def test_canary_baseline_fingerprints_match_their_skeletons() -> None:
    baseline_path = Path(__file__).resolve().parent / "fixtures" / "colab_protocol_baseline.json"
    baseline = json.loads(baseline_path.read_text())
    assert baseline["fingerprints"].keys() == baseline["skeletons"].keys()
    for name, skeleton in baseline["skeletons"].items():
        canonical = json.dumps(skeleton, sort_keys=True)
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        assert fingerprint == baseline["fingerprints"][name], (
            f"{name}: fingerprint is stale relative to its skeleton"
        )
