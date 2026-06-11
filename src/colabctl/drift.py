"""Structural fingerprinting for protocol-drift detection.

The native ``/tun/m/*`` transport's dominant external risk is Google silently changing
a response shape (a renamed key, a new field, a type change). The CLI transport guards
against this with a pinned-version probe; this is the native-side counterpart: reduce a
JSON response to its **shape** (keys + value *types*, values discarded) and hash it, so a
drift changes the fingerprint while ordinary value variation (tokens, endpoints) does not.

Pure and dependency-free so the scheduled canary (``spikes/canary.py``) — and, later,
the transports themselves — can fingerprint a live response and compare it to a committed
baseline, alerting on drift instead of letting users discover breakage.
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


__all__ = ["skeleton_diff", "structural_fingerprint", "structural_skeleton"]
