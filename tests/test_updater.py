from __future__ import annotations

import sys
import time
from pathlib import Path

from backend.updater import (
    CHECK_INTERVAL_SECONDS,
    _install_binary,
    _restore,
    _runs_ok,
    asset_name,
    due_for_check,
    rollback,
    target_path,
)


def test_due_for_check_when_never_run():
    assert due_for_check({}) is True


def test_not_due_immediately_after_a_check():
    assert due_for_check({"last_check": time.time()}) is False


def test_due_again_after_the_interval():
    assert due_for_check({"last_check": time.time() - CHECK_INTERVAL_SECONDS - 1}) is True


def test_due_for_check_tolerates_junk():
    assert due_for_check({"last_check": 0}) is True


def test_asset_name_matches_the_platform():
    expected = {"win32": "yt-dlp.exe", "darwin": "yt-dlp_macos"}.get(sys.platform, "yt-dlp")
    assert asset_name() == expected


def test_target_path_is_in_the_writable_user_dir():
    path = target_path()
    assert path.parent.name == "bin"
    assert path.parent.exists()
    assert path.name.startswith("yt-dlp")


async def test_missing_binary_ignores_the_check_interval(settings, monkeypatch, tmp_path):
    """The packaged build ships without yt-dlp.

    A state file written by a dev run -- where the pip-installed module reports
    a version but `ytdlp_binary()` is None -- must not convince a fresh install
    that there is nothing to fetch, or it sits broken for twelve hours.
    """
    from backend import updater

    monkeypatch.setattr(updater, "_state_path", lambda: tmp_path / "update-state.json")
    (tmp_path / "update-state.json").write_text(
        '{"last_check": %f, "installed_version": "2026.07.04"}' % time.time(), encoding="utf-8"
    )
    settings.ytdlp_path = "/nonexistent/yt-dlp"
    assert settings.ytdlp_binary() is None

    attempted = False

    async def fake_run_update(_settings, _state):
        nonlocal attempted
        attempted = True
        return False

    monkeypatch.setattr(updater, "_run_update", fake_run_update)
    await updater.maybe_update_ytdlp(settings, delay=0)
    assert attempted, "update was skipped despite having no yt-dlp binary"


async def test_recent_check_still_skips_when_a_binary_exists(settings, monkeypatch, tmp_path):
    from backend import updater

    binary = tmp_path / "yt-dlp.exe"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(updater, "_state_path", lambda: tmp_path / "update-state.json")
    (tmp_path / "update-state.json").write_text(
        '{"last_check": %f}' % time.time(), encoding="utf-8"
    )
    settings.ytdlp_path = str(binary)

    async def fail(_settings, _state):
        raise AssertionError("should not have checked")

    monkeypatch.setattr(updater, "_run_update", fail)
    assert await updater.maybe_update_ytdlp(settings, delay=0) is False


# ------------------------------------------------------------ install/rollback


def _dest(tmp_path: Path) -> Path:
    return tmp_path / "yt-dlp.exe"


def test_install_keeps_the_replaced_binary(tmp_path):
    dest = _dest(tmp_path)
    dest.write_bytes(b"old-version")

    backup = _install_binary(b"new-version", dest)

    assert dest.read_bytes() == b"new-version"
    assert backup is not None and backup.read_bytes() == b"old-version"


def test_install_reports_no_backup_on_a_fresh_machine(tmp_path):
    """The packaged build ships nothing in the user dir; the first update has
    nothing to preserve."""
    backup = _install_binary(b"new-version", _dest(tmp_path))
    assert backup is None


def test_restore_puts_the_old_binary_back(tmp_path):
    dest = _dest(tmp_path)
    dest.write_bytes(b"old")
    backup = _install_binary(b"broken", dest)

    _restore(dest, backup)
    assert dest.read_bytes() == b"old"


def test_restore_without_a_backup_removes_the_new_binary(tmp_path):
    """No previous copy: the untrusted download is removed so find_binary falls
    through to the bundled yt-dlp rather than running it."""
    dest = _dest(tmp_path)
    backup = _install_binary(b"broken", dest)  # backup is None
    assert dest.exists()

    _restore(dest, backup)
    assert not dest.exists()


async def test_smoke_test_accepts_a_real_binary():
    """A binary that runs and prints a version passes; the interpreter stands
    in for one."""
    assert await _runs_ok(Path(sys.executable)) is True


async def test_smoke_test_rejects_something_that_will_not_run():
    assert await _runs_ok(Path("/nonexistent/yt-dlp-does-not-exist")) is False


def test_rollback_restores_the_previous_binary(monkeypatch, tmp_path):
    from backend import updater

    dest = _dest(tmp_path)
    monkeypatch.setattr(updater, "target_path", lambda: dest)
    dest.write_bytes(b"old")
    _install_binary(b"new", dest)  # leaves old at .previous, new at dest

    assert rollback() is True
    assert dest.read_bytes() == b"old"


def test_rollback_falls_back_to_bundle_when_no_previous(monkeypatch, tmp_path):
    from backend import updater

    dest = _dest(tmp_path)
    monkeypatch.setattr(updater, "target_path", lambda: dest)
    dest.write_bytes(b"only-copy")  # no .previous beside it

    assert rollback() is True
    assert not dest.exists()


def test_rollback_is_a_no_op_when_nothing_is_installed(monkeypatch, tmp_path):
    from backend import updater

    monkeypatch.setattr(updater, "target_path", lambda: _dest(tmp_path))
    assert rollback() is False


# ------------------------------------------------------------------- channel


def test_release_api_tracks_stable_by_default(settings):
    from backend.updater import STABLE_REPO, release_api

    assert STABLE_REPO in release_api(settings)


def test_release_api_switches_to_nightly(settings):
    from backend.updater import NIGHTLY_REPO, release_api

    settings.ytdlp_channel = "nightly"
    assert NIGHTLY_REPO in release_api(settings)


async def test_force_bypasses_the_check_interval(settings, monkeypatch, tmp_path):
    """A reactive check must run even minutes after the routine one, or a
    mid-session YouTube change waits up to 12h for the fix."""
    from backend import updater

    binary = tmp_path / "yt-dlp.exe"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(updater, "_state_path", lambda: tmp_path / "update-state.json")
    (tmp_path / "update-state.json").write_text('{"last_check": %f}' % time.time(), encoding="utf-8")
    settings.ytdlp_path = str(binary)

    ran = False

    async def fake_run_update(_settings, _state):
        nonlocal ran
        ran = True
        return False

    monkeypatch.setattr(updater, "_run_update", fake_run_update)
    await updater.maybe_update_ytdlp(settings, delay=0, force=True)
    assert ran, "force should have overridden the recent-check short-circuit"
