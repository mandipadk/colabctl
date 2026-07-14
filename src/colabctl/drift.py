"""Structural fingerprinting for protocol-drift detection.

The native ``/tun/m/*`` transport's dominant external risk is Google silently changing
a response shape (a renamed key, a new field, a type change). The CLI transport guards
against this with a pinned-version probe; this is the native-side counterpart: reduce a
JSON response to its **shape** (keys + value *types*, values discarded) and hash it, so a
drift changes the fingerprint while ordinary value variation (tokens, endpoints) does not.

The helpers are pure and dependency-free, so callers can fingerprint a live response and
compare it with a previously reviewed shape before using the response.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

Skeleton = Any  # a JSON value with scalars replaced by their type name


def structural_skeleton(obj: Any) -> Skeleton:
    """Reduce a JSON value to its shape: dicts → sorted keys, lists → element shape, scalars → type.

    ``{"token": "abc", "n": 3}`` and ``{"token": "xyz", "n": 9}`` both become
    ``{"n": "int", "token": "str"}`` — stable across values, sensitive to shape.
    """
    if isinstance(obj, dict):
        return {k: structural_skeleton(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [structural_skeleton(obj[0])] if obj else []
    return type(obj).__name__


def structural_fingerprint(obj: Any) -> str:
    """A short, stable hash of ``obj``'s shape (see :func:`structural_skeleton`)."""
    canonical = json.dumps(structural_skeleton(obj), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def structural_snapshot(
    responses: dict[str, Any],
) -> tuple[dict[str, str | None], dict[str, Skeleton]]:
    """Capture fingerprints and reviewable skeletons without normalizing either twice."""
    fingerprints = {
        name: structural_fingerprint(response) if response is not None else None
        for name, response in responses.items()
    }
    skeletons = {
        name: structural_skeleton(response) if response is not None else None
        for name, response in responses.items()
    }
    return fingerprints, skeletons


def compare_structural_snapshot(
    fingerprints: dict[str, str | None],
    skeletons: dict[str, Skeleton],
    baseline: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Compare a captured snapshot with a reviewed baseline and fail closed on gaps."""
    if baseline is None:
        return False, ["required baseline is missing"]
    baseline_fingerprints = baseline.get("fingerprints")
    baseline_skeletons = baseline.get("skeletons")
    if not isinstance(baseline_fingerprints, dict) or not isinstance(baseline_skeletons, dict):
        return False, ["baseline must contain fingerprint and skeleton mappings"]

    drift: list[str] = []
    for key in sorted(baseline_fingerprints.keys() - fingerprints.keys()):
        drift.append(f"{key} missing from captured responses")
    for key in sorted(fingerprints.keys() - baseline_fingerprints.keys()):
        drift.append(f"{key} has no reviewed baseline")
    for key in sorted(fingerprints.keys() & baseline_fingerprints.keys()):
        if fingerprints[key] != baseline_fingerprints[key]:
            differences = skeleton_diff(baseline_skeletons.get(key), skeletons.get(key))
            drift.append(f"{key} DRIFTED: {differences}")
    return (not drift), (drift or ["shapes match baseline"])


def skeleton_diff(old: Skeleton, new: Skeleton, *, path: str = "") -> list[str]:
    """Human-readable differences between two skeletons (``+added``, ``-removed``, ``~changed``)."""
    diffs: list[str] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(old.keys() - new.keys()):
            diffs.append(f"-{path}{key}")
        for key in sorted(new.keys() - old.keys()):
            diffs.append(f"+{path}{key}")
        for key in sorted(old.keys() & new.keys()):
            diffs.extend(skeleton_diff(old[key], new[key], path=f"{path}{key}."))
    elif isinstance(old, list) and isinstance(new, list):
        if old and new:
            diffs.extend(skeleton_diff(old[0], new[0], path=f"{path}[]."))
        elif old != new:
            diffs.append(f"~{path}[] {old or '[]'}→{new or '[]'}")
    elif old != new:
        diffs.append(f"~{path.rstrip('.') or '<root>'} {old}→{new}")
    return diffs


__all__ = [
    "compare_structural_snapshot",
    "skeleton_diff",
    "structural_fingerprint",
    "structural_skeleton",
    "structural_snapshot",
]
