"""Guards on frontend/ui/i18n.js.

The translation table is plain JS, so nothing in the build would notice a key
added to one locale and forgotten in the other, or a backend message code with
no entry at all -- both surface as English text (or a raw key) in a Greek UI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.config import bundle_dir
from backend.ytdlp import FailureKind

I18N_PATH = bundle_dir() / "frontend" / "ui" / "i18n.js"
KEY = re.compile(r"'([\w.]+)':")


def _locale_block(source: str, locale: str) -> str:
    """Slice out one locale's object literal by brace matching.

    Placeholders like {query} are balanced, so a simple depth count is enough.
    """
    start = source.index(f"{locale}: {{")
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated {locale} block")


@pytest.fixture(scope="module")
def tables() -> dict[str, set[str]]:
    source = I18N_PATH.read_text(encoding="utf-8")
    return {loc: set(KEY.findall(_locale_block(source, loc))) for loc in ("en", "el")}


def test_i18n_file_exists():
    assert I18N_PATH.exists(), f"missing {I18N_PATH}"


def test_locales_define_the_same_keys(tables):
    assert tables["en"] == tables["el"], (
        f"only in en: {sorted(tables['en'] - tables['el'])}; "
        f"only in el: {sorted(tables['el'] - tables['en'])}"
    )


def test_every_failure_kind_is_translatable(tables):
    """resolver.py and ytdlp.py derive codes from FailureKind, so a new kind
    must not silently render as a raw key."""
    for kind in FailureKind:
        assert f"resolve.{kind.value}" in tables["en"], f"no resolve.{kind.value}"
        assert f"error.{kind.value}" in tables["en"], f"no error.{kind.value}"


def test_engine_notice_codes_are_translatable(tables):
    """Every code engine.py emits must have a string in both locales."""
    engine = (bundle_dir() / "backend" / "engine.py").read_text(encoding="utf-8")
    codes = set(re.findall(r'"(notice\.[a-z_]+)"', engine))
    assert codes, "expected engine.py to emit notice codes"
    missing = codes - tables["en"]
    assert not missing, f"engine.py emits untranslated codes: {sorted(missing)}"


def test_greek_strings_are_actually_greek(tables):
    """A copy-paste that leaves English text in the el table defeats the point."""
    source = I18N_PATH.read_text(encoding="utf-8")
    block = _locale_block(source, "el")
    # Keys that are pure placeholders or technical proper nouns ("PO token")
    # have no translatable prose.
    exempt = {"notice.resolve_failed", "notice.player_unavailable", "app.title", "settings.diag.potoken", "settings.diag.ffmpeg"}
    for key, value in re.findall(r"'([\w.]+)':\s*\n?\s*'([^']*)'", block):
        if key in exempt or not value.strip():
            continue
        assert re.search(r"[Ͱ-Ͽ]", value), f"{key} is not translated: {value!r}"
