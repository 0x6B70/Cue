"""The karaoke bar and intermission card (backend/overlay.py).

Pure string composition, so no mpv is needed; tests/test_player_integration.py
covers drawing in a real window.
"""

from __future__ import annotations

from backend.overlay import (
    OverlayTheme,
    UpcomingEntry,
    ass_color,
    compose_bar,
    compose_intermission,
    fit,
    format_clock,
)


def _bar(entries, **theme):
    return "\n".join(compose_bar(entries, OverlayTheme(**theme)))


def test_ass_color_is_bgr_and_rejects_garbage():
    assert ass_color("#112233") == "&H332211&"
    assert ass_color("not a colour", "#ffffff") == "&HFFFFFF&"


def test_bar_shows_singer_title_or_both():
    entries = [UpcomingEntry("Song", "Maria")]
    both = _bar(entries, label_mode="both")
    assert "}Maria" in both and "}Song" in both
    singer = _bar(entries, label_mode="singer")
    assert "}Maria" in singer and "}Song" not in singer
    title = _bar(entries, label_mode="title")
    assert "}Song" in title and "Maria" not in title


def test_singer_mode_falls_back_to_the_title():
    assert "}Song" in _bar([UpcomingEntry("Song")], label_mode="singer")


def test_bar_draws_one_box_per_slot_with_placeholders():
    text = _bar([UpcomingEntry("A")], boxes=4)
    for n in "1234":
        assert "}" + n in text
    assert text.count("}—") == 3


def test_bar_escapes_markup_in_names():
    assert "{\\fs90}" not in _bar([UpcomingEntry("x", "{\\fs90}evil")])


def test_bar_timer_and_brand():
    text = "\n".join(compose_bar([], OverlayTheme(brand="Karaoke"), timer="03:12"))
    assert "[03:12]" in text and "}Karaoke" in text


def test_intermission_countdown_and_heading_follow_locale():
    text = "\n".join(compose_intermission(UpcomingEntry("Song", "Makis"), 2.4, 10, OverlayTheme(locale="el")))
    assert "ΕΠΟΜΕΝΟΣ ΣΤΗ ΣΚΗΝΗ" in text
    assert "}Makis" in text
    assert "σε 3" in text  # 2.4s left rounds up


def test_custom_headings_override_the_locale():
    text = "\n".join(compose_intermission(UpcomingEntry("Song"), 1, 5, OverlayTheme(stage_heading="ON STAGE")))
    assert "ON STAGE" in text


def test_fit_and_clock():
    assert fit("a" * 200, 100, 20).endswith("…")
    assert fit("short", 500, 20) == "short"
    assert format_clock(192) == "03:12"
    assert format_clock(3700) == "1:01:40"



def test_logo_reserves_room_and_replaces_the_brand_on_the_card():
    from backend.overlay import bar_logo_box, card_logo_box

    theme = OverlayTheme(brand="Karaoke")
    plain = compose_bar([UpcomingEntry("A")], theme)
    with_logo = compose_bar([UpcomingEntry("A")], theme, logo_width=bar_logo_box((300, 150), theme)[2])
    assert plain != with_logo  # boxes start further right
    card = "\n".join(compose_intermission(UpcomingEntry("A"), 1, 5, theme, logo=True))
    assert "}Karaoke" not in card
    x, y, w, h = card_logo_box((1000, 100), 1280)
    assert w <= 1280 * 0.6 and abs(w / h - 10) < 0.01
