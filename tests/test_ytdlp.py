from __future__ import annotations

import sys

import pytest

from backend.ytdlp import FailureKind, YtDlp, YtDlpError, classify


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ERROR: Sign in to confirm you're not a bot.", FailureKind.BOT_CHECK),
        ("HTTP Error 429: Too Many Requests", FailureKind.BOT_CHECK),
        ("ERROR: Video unavailable", FailureKind.UNAVAILABLE),
        ("This video is private video", FailureKind.UNAVAILABLE),
        ("Sign in to confirm your age", FailureKind.AGE_RESTRICTED),
        ("urlopen error Tunnel connection failed", FailureKind.NETWORK),
        ("ERROR: This video is DRM protected", FailureKind.DRM),
        ("ERROR: Requested format is not available", FailureKind.NO_FORMATS),
        ("WARNING: Only images are available for download", FailureKind.NO_FORMATS),
        ("yt-dlp: error: no such option: --remote-components", FailureKind.BAD_OPTION),
        ("error: unrecognized arguments: --js-runtimes", FailureKind.BAD_OPTION),
        ("something else entirely", FailureKind.OTHER),
    ],
)
def test_classify(text, expected):
    assert classify(text) is expected


@pytest.mark.parametrize("kind", [FailureKind.DRM, FailureKind.NO_FORMATS])
def test_per_client_failures_are_not_treated_as_unavailable(kind):
    """DRM and empty format lists are verdicts on a *client*, not the video.

    YouTube runs DRM and SABR-only experiments per session and per client, so
    the resolver has to keep walking the chain -- only UNAVAILABLE aborts it.
    """
    assert kind is not FailureKind.UNAVAILABLE
    assert YtDlpError("x", "", kind=kind).user_message != YtDlpError(
        "x", "", kind=FailureKind.UNAVAILABLE
    ).user_message


def test_bot_check_beats_age_check_when_both_match():
    # "Sign in to confirm you're not a bot" must not be read as an age gate.
    assert classify("Sign in to confirm you're not a bot") is FailureKind.BOT_CHECK


async def test_version_reads_stub(ytdlp, monkeypatch):
    monkeypatch.setenv("STUB_VERSION", "2030.01.01")
    assert await ytdlp.version() == "2030.01.01"


async def test_missing_binary_reports_not_found(settings, monkeypatch):
    ytdlp = YtDlp(settings)
    monkeypatch.setattr(ytdlp, "_binary", lambda: ["/nonexistent/yt-dlp-does-not-exist"])
    with pytest.raises(YtDlpError) as excinfo:
        await ytdlp.run_json(["-J", "x"])
    assert excinfo.value.kind is FailureKind.NOT_FOUND


async def test_unresolvable_override_falls_back_to_module(settings):
    """A bad ytdlp_path must not hard-fail while a usable interpreter exists."""
    settings.ytdlp_path = "/nonexistent/yt-dlp-does-not-exist"
    assert settings.ytdlp_binary() is None
    assert YtDlp(settings)._binary()[0] == sys.executable


async def test_ignore_config_is_always_passed(ytdlp):
    assert "--ignore-config" in ytdlp.base_args()


def test_warnings_are_not_suppressed(ytdlp):
    """yt-dlp's warnings are the only explanation of why a client came back
    empty ("requires a GVS PO Token", "SABR-only experiment", "DRM")."""
    assert "--no-warnings" not in ytdlp.base_args()


def test_js_runtime_is_passed_when_available(settings, tmp_path):
    """Signature and n-challenge solving both need a runtime *and* the solver
    script; a runtime alone still fails. --ignore-config means the user's own
    yt-dlp config cannot supply either, so we must."""
    fake = tmp_path / "deno.exe"
    fake.write_text("", encoding="utf-8")
    settings.js_runtime_path = str(fake)
    args = YtDlp(settings).base_args()
    assert f"deno:{fake}" in args
    assert "--js-runtimes" in args
    assert "--remote-components" in args and "ejs:github" in args


def test_js_runtime_omitted_when_absent(settings):
    settings.js_runtime_path = "/nonexistent/deno"
    assert "--js-runtimes" not in YtDlp(settings).base_args()


def test_js_runtime_label_follows_the_binary(settings, tmp_path):
    """yt-dlp needs RUNTIME[:PATH] where RUNTIME names a runtime it knows, so
    a node binary must not be announced as deno."""
    fake = tmp_path / "node.exe"
    fake.write_text("", encoding="utf-8")
    settings.js_runtime_path = str(fake)
    assert f"node:{fake}" in YtDlp(settings).base_args()


def test_frozen_build_does_not_relaunch_itself(settings, monkeypatch):
    """sys.executable is the app when frozen, so the "-m yt_dlp" dev fallback
    would start a second backend per resolve instead of running yt-dlp."""
    settings.ytdlp_path = "/nonexistent/yt-dlp"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    with pytest.raises(YtDlpError) as excinfo:
        YtDlp(settings)._binary()
    assert excinfo.value.kind is FailureKind.NOT_FOUND


async def test_frozen_version_reports_none_instead_of_raising(settings, monkeypatch):
    settings.ytdlp_path = "/nonexistent/yt-dlp"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert await YtDlp(settings).version() is None


def test_remote_components_can_be_disabled(settings):
    settings.ejs_remote_components = False
    assert "--remote-components" not in YtDlp(settings).base_args()


async def test_bad_option_is_survived_by_dropping_the_flag(ytdlp, monkeypatch):
    """A yt-dlp update that drops a flag we pass must not kill every resolve.

    The challenge-solver flags are the newest and likeliest to churn. If one
    disappears, yt-dlp exits on a parse error before any network work -- so
    without this the failure looks exactly like YouTube going down, on every
    single video at once.
    """
    monkeypatch.setenv("STUB_REJECT_OPTION", "--remote-components")
    assert "--remote-components" in ytdlp.base_args()

    # The first attempt hits the rejected flag; the retry drops it and succeeds.
    info = await ytdlp.run_json(["-J", "-f", "b", "https://www.youtube.com/watch?v=abc"])
    assert info["id"] == "abc"
    assert ytdlp.js_runtime_disabled is True


async def test_the_flag_drop_latches_for_the_process(ytdlp, monkeypatch):
    """Once dropped, the flag is not paid for again -- later calls omit it up
    front rather than failing and retrying every single time."""
    monkeypatch.setenv("STUB_REJECT_OPTION", "--remote-components")
    await ytdlp.run_json(["-J", "-f", "b", "https://www.youtube.com/watch?v=abc"])

    assert ytdlp.js_runtime_disabled is True
    assert "--remote-components" not in ytdlp.base_args()
    assert ytdlp.js_runtime_args() == []


async def test_a_real_failure_is_not_mistaken_for_a_bad_option(ytdlp, monkeypatch):
    """The retry must fire only for an option error, not swallow a genuine
    failure by blaming the flags and pressing on."""
    monkeypatch.setenv("STUB_FAIL_MODE", "bot")
    with pytest.raises(YtDlpError) as excinfo:
        await ytdlp.run_json(["-J", "-f", "b", "https://www.youtube.com/watch?v=abc"])
    assert excinfo.value.kind is FailureKind.BOT_CHECK
    assert ytdlp.js_runtime_disabled is False


def test_cookie_args_prefers_browser(settings):
    settings.cookies_from_browser = "firefox"
    settings.cookies_file = "/tmp/cookies.txt"
    assert YtDlp(settings).cookie_args() == ["--cookies-from-browser", "firefox"]


def test_cookie_args_ignores_missing_file(settings):
    settings.cookies_file = "/nonexistent/cookies.txt"
    assert YtDlp(settings).cookie_args() == []
