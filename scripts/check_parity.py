#!/usr/bin/env python3
"""Check version parity between the Python and TypeScript Noukai SDKs.

Both SDKs share a wire protocol and should stay released in lockstep. This
script reads the top released entry of each ``CHANGELOG.md`` and compares
them, exiting non-zero if they disagree so a human can either port the
missing changes or explicitly accept the drift.

Usage (from the Python SDK repo root):

    python scripts/check_parity.py

    # Or point at a specific sibling checkout
    python scripts/check_parity.py --ts-repo ../noukai-typescript-sdk

Assumes the sibling TypeScript SDK is checked out at ``../noukai-typescript-sdk``
by default — matching the layout ``noukai-sdk/{noukai-python-sdk,noukai-typescript-sdk}/``
already used for local development.

Exit codes:
    0 — versions match
    1 — versions differ (drift detected)
    2 — a CHANGELOG could not be read/parsed (misconfiguration)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ``## [X.Y.Z] — YYYY-MM-DD`` — the em-dash character is the one used by both
# CHANGELOGs (U+2014). ASCII hyphen is accepted as a fallback for robustness.
_RELEASED_HEADING = re.compile(
    r"^##\s+\[(?P<version>\d+\.\d+\.\d+)\]\s+[—-]\s+(?P<date>\d{4}-\d{2}-\d{2})",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Release:
    version: str
    date: str

    def version_tuple(self) -> tuple[int, ...]:
        return tuple(int(p) for p in self.version.split("."))


def parse_latest_release(changelog_path: Path) -> Release | None:
    """Return the first released entry in ``changelog_path`` (skipping
    ``[Unreleased]``), or ``None`` if none is found."""
    try:
        text = changelog_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: could not read {changelog_path}: {exc}", file=sys.stderr)
        return None
    match = _RELEASED_HEADING.search(text)
    if match is None:
        return None
    return Release(version=match["version"], date=match["date"])


def _print_top_hunk(changelog_path: Path, version: str) -> None:
    """Print the CHANGELOG section for ``version`` to stderr as a nudge for
    the user reviewing drift. Bounded at ~40 lines to keep output digestible."""
    try:
        text = changelog_path.read_text(encoding="utf-8")
    except OSError:
        return
    start_marker = f"## [{version}]"
    start = text.find(start_marker)
    if start == -1:
        return
    # Slice from the heading to just before the next ``## `` heading.
    end = text.find("\n## ", start + len(start_marker))
    if end == -1:
        end = len(text)
    hunk = text[start:end].rstrip()
    lines = hunk.splitlines()
    if len(lines) > 40:
        lines = lines[:40] + ["... (truncated)"]
    for line in lines:
        print(f"  {line}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--py-repo",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Path to the Python SDK repo root (default: parent of this script).",
    )
    parser.add_argument(
        "--ts-repo",
        type=Path,
        default=None,
        help="Path to the TypeScript SDK repo root (default: ../noukai-typescript-sdk).",
    )
    args = parser.parse_args()

    py_repo: Path = args.py_repo.resolve()
    ts_repo: Path = (args.ts_repo or py_repo.parent / "noukai-typescript-sdk").resolve()

    py_changelog = py_repo / "CHANGELOG.md"
    ts_changelog = ts_repo / "CHANGELOG.md"

    py_release = parse_latest_release(py_changelog)
    ts_release = parse_latest_release(ts_changelog)

    if py_release is None:
        print(f"error: no released entry found in {py_changelog}", file=sys.stderr)
        return 2
    if ts_release is None:
        print(f"error: no released entry found in {ts_changelog}", file=sys.stderr)
        return 2

    print(f"  Py SDK: {py_release.version} ({py_release.date})")
    print(f"  TS SDK: {ts_release.version} ({ts_release.date})")

    if py_release.version == ts_release.version:
        print("  ✓ In sync")
        return 0

    # Compare so we can tell the user WHICH side is ahead.
    py_tuple = py_release.version_tuple()
    ts_tuple = ts_release.version_tuple()
    if ts_tuple > py_tuple:
        ahead, behind = "TS", "Py"
        ahead_hunk = (ts_changelog, ts_release.version)
    else:
        ahead, behind = "Py", "TS"
        ahead_hunk = (py_changelog, py_release.version)

    print(f"  ✗ {ahead} ahead of {behind}", file=sys.stderr)
    print(
        f"\nLatest {ahead} CHANGELOG section — review for port candidates:\n",
        file=sys.stderr,
    )
    _print_top_hunk(*ahead_hunk)
    return 1


if __name__ == "__main__":
    sys.exit(main())
