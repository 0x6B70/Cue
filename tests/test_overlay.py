"""Pure-Python checks on the overlay ASS composition.

Unlike test_player_integration these need no mpv -- compose_upcoming only builds
a string -- so they always run.
"""

from __future__ import annotations

from backend.player import (
    OVERLAY_ALIGNMENTS,
    OVERLAY_DEFAULT_POSITION,
    OVERLAY_FONT_SIZE,
    compose_upcoming,
)


def test_defaults_are_bottom_right_at_the_default_size():
    style = compose_upcoming(["Song"])
    assert f"\\an{OVERLAY_ALIGNMENTS['bottom-right']}" in style
    assert f"\\fs{OVERLAY_FONT_SIZE}" in style


def test_each_position_selects_its_alignment_code():
    for name, code in OVERLAY_ALIGNMENTS.items():
        assert f"\\an{code}\\" in compose_upcoming(["Song"], position=name)


def test_unknown_position_falls_back_to_the_default():
    fallback = OVERLAY_ALIGNMENTS[OVERLAY_DEFAULT_POSITION]
    assert f"\\an{fallback}\\" in compose_upcoming(["Song"], position="nonsense")


def test_font_size_lands_in_the_style():
    assert "\\fs52" in compose_upcoming(["Song"], font_size=52)
