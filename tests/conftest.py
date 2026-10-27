"""Test fixtures.

The user data dir is redirected to a tmp path *before* backend modules are
imported, because several of them resolve paths at import time.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP_HOME = Path(tempfile.mkdtemp(prefix="yqp-tests-"))
os.environ["XDG_DATA_HOME"] = str(_TMP_HOME / "share")
os.environ["APPDATA"] = str(_TMP_HOME / "appdata")
os.environ["HOME"] = str(_TMP_HOME)

import pytest  # noqa: E402

STUB_SOURCE = '''#!/usr/bin/env python3
"""Stand-in for the yt-dlp binary, driven by environment variables."""
import json, os, sys

argv = sys.argv[1:]

if "--version" in argv:
    print(os.environ.get("STUB_VERSION", "2026.07.04"))
    sys.exit(0)

# Simulate a yt-dlp release that dropped a flag: if any option named in
# STUB_REJECT_OPTION is on the command line, exit the way argparse does for an
# unknown option (exit 2, "no such option" on stderr) before doing anything
# else. Lets a test prove the resolver survives a flag removal.
for _opt in [o for o in os.environ.get("STUB_REJECT_OPTION", "").split(",") if o]:
    if _opt in argv:
        sys.stderr.write(f"yt-dlp: error: no such option: {_opt}\\n")
        sys.exit(2)

# Which client did the caller ask for? "default" when unspecified.
client = "default"
for i, arg in enumerate(argv):
    if arg == "--extractor-args" and i + 1 < len(argv):
        for part in argv[i + 1].split(":", 1)[-1].split(";"):
            if part.startswith("player_client="):
                client = part.split("=", 1)[1]

failing = [c for c in os.environ.get("STUB_FAIL_CLIENTS", "").split(",") if c]
mode = os.environ.get("STUB_FAIL_MODE", "")

def fail(kind):
    messages = {
        "bot": "ERROR: [youtube] abc: Sign in to confirm you are not a bot.",
        "unavailable": "ERROR: [youtube] abc: Video unavailable",
        "network": "ERROR: unable to download webpage: urlopen error",
    }
    sys.stderr.write(messages.get(kind, "ERROR: something went wrong") + "\\n")
    sys.exit(1)

if mode and not failing:
    fail(mode)
if client in failing:
    fail(mode or "bot")

query = argv[-1]

if query.startswith("ytsearch"):
    count = int("".join(c for c in query.split(":")[0] if c.isdigit()) or 5)
    entries = [
        {
            "_type": "url",
            "id": f"vid{i:03d}",
            "title": f"Result {i}",
            "channel": f"Channel {i}",
            "duration": 100 + i,
            "thumbnails": [{"url": f"https://i.ytimg.com/vi/vid{i:03d}/mq.jpg", "width": 320}],
            "live_status": "not_live",
        }
        for i in range(count)
    ]
    print(json.dumps({"_type": "playlist", "entries": entries}))
    sys.exit(0)

video_id = query.rsplit("v=", 1)[-1]
print(json.dumps({
    "id": video_id,
    "title": f"Video {video_id}",
    "duration": 212.0,
    "requested_formats": [
        {
            "url": f"https://stub.googlevideo.test/{video_id}/video?client={client}",
            "vcodec": "avc1", "acodec": "none",
            "http_headers": {"User-Agent": "stub-agent", "Accept": "*/*"},
        },
        {
            "url": f"https://stub.googlevideo.test/{video_id}/audio",
            "vcodec": "none", "acodec": "mp4a",
            "http_headers": {"User-Agent": "stub-agent"},
        },
    ],
}))
'''


_FORWARDER_TEMPLATE = '''using System;
using System.Diagnostics;
using System.Text;

class Forwarder {
    static string Quote(string s) {
        var sb = new StringBuilder();
        sb.Append('"');
        for (int i = 0; i < s.Length; i++) {
            int backslashes = 0;
            while (i < s.Length && s[i] == '\\\\') { backslashes++; i++; }
            if (i == s.Length) {
                sb.Append('\\\\', backslashes * 2);
                break;
            } else if (s[i] == '"') {
                sb.Append('\\\\', backslashes * 2 + 1);
                sb.Append('"');
            } else {
                sb.Append('\\\\', backslashes);
                sb.Append(s[i]);
            }
        }
        sb.Append('"');
        return sb.ToString();
    }

    static int Main(string[] args) {
        var psi = new ProcessStartInfo();
        psi.FileName = @"__PYEXE__";
        var sb = new StringBuilder();
        sb.Append(Quote(@"__STUBPY__"));
        foreach (var a in args) {
            sb.Append(' ');
            sb.Append(Quote(a));
        }
        psi.Arguments = sb.ToString();
        psi.UseShellExecute = false;
        var p = Process.Start(psi);
        p.WaitForExit();
        return p.ExitCode;
    }
}
'''


def _find_csc() -> str | None:
    for candidate in (
        r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return shutil.which("csc")


@pytest.fixture(scope="session")
def stub_ytdlp() -> str:
    path = _TMP_HOME / "stub-yt-dlp"
    path.write_text(STUB_SOURCE, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    if sys.platform == "win32":
        # A .bat/.cmd wrapper is executed by an implicit cmd.exe layer, which
        # re-parses the whole command line for shell metacharacters such as
        # `<` and `>` -- both of which appear in yt-dlp format selectors
        # (e.g. "bv*[height<=?1080]"). That mangles real invocations, so we
        # compile a tiny native forwarder instead: it hands argv straight to
        # CreateProcess, which never interprets redirection tokens.
        csc = _find_csc()
        if csc is None:  # pragma: no cover - dev machines always have .NET
            raise RuntimeError("no C# compiler (csc.exe) found to build the test stub launcher")
        cs_src = _FORWARDER_TEMPLATE.replace("__PYEXE__", sys.executable).replace(
            "__STUBPY__", str(path)
        )
        cs_file = _TMP_HOME / "Forwarder.cs"
        cs_file.write_text(cs_src, encoding="utf-8")
        exe_file = _TMP_HOME / "stub-yt-dlp.exe"
        result = subprocess.run(
            [csc, "/nologo", f"/out:{exe_file}", str(cs_file)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:  # pragma: no cover - compiler failures are unexpected
            raise RuntimeError(f"failed to compile stub launcher: {result.stdout}\n{result.stderr}")
        return str(exe_file)
    return str(path)


@pytest.fixture
def settings(stub_ytdlp, monkeypatch):
    from backend.config import Settings

    monkeypatch.delenv("STUB_FAIL_MODE", raising=False)
    monkeypatch.delenv("STUB_FAIL_CLIENTS", raising=False)
    # verify_streams off: the stub's URLs point nowhere, so the playability
    # probe would fail every resolve. tests/test_resolver.py covers the probe.
    created = Settings(
        ytdlp_path=stub_ytdlp, ytdlp_auto_update=False, invidious_fallback=False, verify_streams=False
    )
    # The stub is a python script, not a real binary; run it through the
    # interpreter on platforms where the shebang will not be honoured.
    return created


@pytest.fixture
def ytdlp(settings):
    from backend.ytdlp import YtDlp

    return YtDlp(settings)
