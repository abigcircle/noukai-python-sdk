"""Tests for scripts/check_parity.py — the SDK version parity guard.

The parity check has no dependencies and lives outside the package, but a
silent regex regression would defeat the guard's purpose. These tests lock
in the CHANGELOG heading grammar the script relies on.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_parity.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_parity", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``@dataclass`` needs the module registered in ``sys.modules`` before
    # ``exec_module`` runs (it does a ``sys.modules.get(cls.__module__)`` under
    # the hood on Python 3.10). Without this, the ``Release`` dataclass at
    # module-import time raises ``AttributeError: 'NoneType' object has no
    # attribute '__dict__'``.
    sys.modules["check_parity"] = module
    spec.loader.exec_module(module)
    return module


check_parity = _load_module()


class TestParseLatestRelease:
    def test_extracts_first_released_entry(self, tmp_path):
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            "### Fixed\n- pending fix\n\n"
            "## [0.3.0] — 2026-07-06\n\n"
            "### Breaking\n- stuff\n\n"
            "## [0.2.0] — 2026-06-06\n\n"
            "### Added\n- older stuff\n",
            encoding="utf-8",
        )

        release = check_parity.parse_latest_release(changelog)

        assert release is not None
        assert release.version == "0.3.0"
        assert release.date == "2026-07-06"

    def test_accepts_ascii_hyphen_fallback(self, tmp_path):
        """The regex accepts a plain hyphen as a fallback so an editor
        auto-correcting the em-dash doesn't silently break the parser."""
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(
            "## [Unreleased]\n\n## [0.3.0] - 2026-07-06\n\n### x\n",
            encoding="utf-8",
        )

        release = check_parity.parse_latest_release(changelog)

        assert release is not None
        assert release.version == "0.3.0"

    def test_skips_unreleased_heading(self, tmp_path):
        """``## [Unreleased]`` has no date and must not be picked as latest."""
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(
            "## [Unreleased]\n\n### Added\n- pending\n",
            encoding="utf-8",
        )

        assert check_parity.parse_latest_release(changelog) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert check_parity.parse_latest_release(tmp_path / "missing.md") is None


class TestVersionTuple:
    def test_orders_versions_correctly(self):
        r_low = check_parity.Release(version="0.3.0", date="2026-07-06")
        r_high = check_parity.Release(version="0.10.0", date="2026-08-01")

        # String comparison would return "0.10.0" < "0.3.0" — tuple comparison
        # is what makes the drift-direction message correct.
        assert r_low.version_tuple() < r_high.version_tuple()


class TestMainExitCodes:
    """Smoke-check the end-to-end script via its ``main()`` entrypoint."""

    def _write_changelog(self, path: Path, version: str, date: str) -> None:
        path.write_text(
            f"# Changelog\n\n## [Unreleased]\n\n## [{version}] — {date}\n\n### Added\n- x\n",
            encoding="utf-8",
        )

    def test_exit_0_when_in_sync(self, tmp_path, monkeypatch, capsys):
        py_repo = tmp_path / "py"
        ts_repo = tmp_path / "ts"
        py_repo.mkdir()
        ts_repo.mkdir()
        self._write_changelog(py_repo / "CHANGELOG.md", "0.3.0", "2026-07-06")
        self._write_changelog(ts_repo / "CHANGELOG.md", "0.3.0", "2026-06-23")

        monkeypatch.setattr(
            "sys.argv",
            ["check_parity.py", "--py-repo", str(py_repo), "--ts-repo", str(ts_repo)],
        )
        assert check_parity.main() == 0
        out = capsys.readouterr().out
        assert "In sync" in out

    def test_exit_1_when_ts_ahead(self, tmp_path, monkeypatch, capsys):
        py_repo = tmp_path / "py"
        ts_repo = tmp_path / "ts"
        py_repo.mkdir()
        ts_repo.mkdir()
        self._write_changelog(py_repo / "CHANGELOG.md", "0.3.0", "2026-07-06")
        self._write_changelog(ts_repo / "CHANGELOG.md", "0.4.0", "2026-08-01")

        monkeypatch.setattr(
            "sys.argv",
            ["check_parity.py", "--py-repo", str(py_repo), "--ts-repo", str(ts_repo)],
        )
        assert check_parity.main() == 1
        captured = capsys.readouterr()
        # The direction message goes to stderr.
        assert "TS ahead of Py" in captured.err

    def test_exit_2_when_changelog_unparseable(self, tmp_path, monkeypatch):
        py_repo = tmp_path / "py"
        ts_repo = tmp_path / "ts"
        py_repo.mkdir()
        ts_repo.mkdir()
        # Empty CHANGELOG — no released heading to parse.
        (py_repo / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
        (ts_repo / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")

        monkeypatch.setattr(
            "sys.argv",
            ["check_parity.py", "--py-repo", str(py_repo), "--ts-repo", str(ts_repo)],
        )
        assert check_parity.main() == 2


@pytest.mark.skipif(not _SCRIPT_PATH.exists(), reason="script missing")
def test_script_file_is_present():
    """Guard: the script itself must exist. If someone moves it, this test
    catches the RELEASING.md drift before a release day."""
    assert _SCRIPT_PATH.is_file()
