from __future__ import annotations

import sys

import pytest

from backend.config import find_binary


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows shim handling")


@pytest.fixture(autouse=True)
def _without_bundled_vendor(tmp_path, monkeypatch):
    """Isolate these tests from a populated vendor/ in the working tree.

    A bundled binary deliberately outranks PATH, which would otherwise mask
    every discovery path exercised below.
    """
    monkeypatch.setattr("backend.config.bundle_dir", lambda: tmp_path / "no-bundle")


def test_cmd_shim_is_rejected(tmp_path, monkeypatch):
    """A .cmd/.bat shim cannot be invoked safely on Windows.

    CreateProcess cannot execute a batch file directly, so Windows wraps it in
    an implicit `cmd.exe /c`, which re-parses the command line for shell
    metacharacters. Our yt-dlp format selector contains `<`
    (`bv*[height<=?1080]`), which cmd reads as input redirection, and the call
    dies with "The filename, directory name, or volume label syntax is
    incorrect" before yt-dlp ever runs.
    """
    shim = tmp_path / "yt-dlp.cmd"
    shim.write_text("@echo off\r\n", encoding="utf-8")
    monkeypatch.setattr("backend.config.shutil.which", lambda name: str(shim))
    assert find_binary("yt-dlp") is None


def test_exe_beside_a_shim_is_preferred(tmp_path, monkeypatch):
    shim = tmp_path / "yt-dlp.cmd"
    shim.write_text("@echo off\r\n", encoding="utf-8")
    real = tmp_path / "yt-dlp.exe"
    real.write_text("", encoding="utf-8")
    monkeypatch.setattr("backend.config.shutil.which", lambda name: str(shim))
    assert find_binary("yt-dlp") == str(real)


def test_plain_exe_is_returned_unchanged(tmp_path, monkeypatch):
    real = tmp_path / "yt-dlp.exe"
    real.write_text("", encoding="utf-8")
    monkeypatch.setattr("backend.config.shutil.which", lambda name: str(real))
    assert find_binary("yt-dlp") == str(real)


def test_shim_override_is_also_rejected(tmp_path):
    """An explicit ytdlp_path pointing at a shim is no safer than a PATH hit."""
    shim = tmp_path / "yt-dlp.cmd"
    shim.write_text("@echo off\r\n", encoding="utf-8")
    assert find_binary("yt-dlp", str(shim)) is None


def test_bundled_binary_beats_whatever_is_on_the_target_machine(tmp_path, monkeypatch):
    """The installer ships mpv, yt-dlp and deno under vendor/.

    A machine with its own copy on PATH must still get the version shipped and
    tested with the app, rather than an arbitrary older one.
    """
    import backend.config as config

    vendor = tmp_path / "bundle" / "vendor"
    vendor.mkdir(parents=True)
    shipped = vendor / "mpv.exe"
    shipped.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "bundle_dir", lambda: tmp_path / "bundle")
    monkeypatch.setattr(config.shutil, "which", lambda name: r"C:\somewhere\else\mpv.exe")
    assert find_binary("mpv") == str(shipped)


def test_com_wrapper_is_swapped_for_the_real_exe(tmp_path, monkeypatch):
    """PATHEXT puts .COM ahead of .EXE, so which() finds mpv.com first.

    mpv.com is a console wrapper that re-executes mpv.exe as a child, so
    terminating it leaves the real player orphaned -- processes pile up one per
    session.
    """
    import backend.config as config

    com = tmp_path / "mpv.com"
    com.write_text("", encoding="utf-8")
    exe = tmp_path / "mpv.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(config.shutil, "which", lambda name: str(com))
    assert find_binary("mpv") == str(exe)


def test_lone_com_is_still_usable(tmp_path, monkeypatch):
    """A .com with no .exe beside it is a real executable, not a stub."""
    import backend.config as config

    com = tmp_path / "tool.com"
    com.write_text("", encoding="utf-8")
    monkeypatch.setattr(config.shutil, "which", lambda name: str(com))
    assert find_binary("tool") == str(com)


def test_mpv_is_found_where_installers_put_it(tmp_path, monkeypatch):
    """`winget install shinchiro.mpv` never touches PATH.

    Without this the app tells people to install mpv that they have already
    installed, and the only way out is hand-editing settings.json.
    """
    import backend.config as config

    installed = tmp_path / "MPV Player" / "mpv.exe"
    installed.parent.mkdir(parents=True)
    installed.write_text("", encoding="utf-8")

    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    monkeypatch.setitem(config._WINDOWS_HINTS, "mpv", (str(installed),))
    assert find_binary("mpv") == str(installed)


def test_path_wins_over_the_install_hints(tmp_path, monkeypatch):
    """A deliberately installed copy on PATH beats a guessed location."""
    import backend.config as config

    on_path = tmp_path / "chosen" / "mpv.exe"
    on_path.parent.mkdir(parents=True)
    on_path.write_text("", encoding="utf-8")
    hinted = tmp_path / "MPV Player" / "mpv.exe"
    hinted.parent.mkdir(parents=True)
    hinted.write_text("", encoding="utf-8")

    monkeypatch.setattr(config.shutil, "which", lambda name: str(on_path))
    monkeypatch.setitem(config._WINDOWS_HINTS, "mpv", (str(hinted),))
    assert find_binary("mpv") == str(on_path)


def test_missing_hint_locations_are_skipped(monkeypatch):
    import backend.config as config

    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    monkeypatch.setitem(
        config._WINDOWS_HINTS, "mpv", (r"%NO_SUCH_VAR%\mpv.exe", r"C:\nope\mpv.exe")
    )
    assert find_binary("mpv") is None
