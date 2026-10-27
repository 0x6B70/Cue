"""HTTP surface tests.

`backend.main` builds its app at import time, so these drive that module
directly rather than constructing a second app.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import main as main_module
from tests.test_engine import FakePlayer


@pytest.fixture
def client(settings, monkeypatch, tmp_path, stub_ytdlp):
    monkeypatch.setattr("backend.engine.MpvPlayer", FakePlayer)
    monkeypatch.setattr(main_module.settings, "ytdlp_path", stub_ytdlp)
    monkeypatch.setattr(main_module.settings, "ytdlp_auto_update", False)
    monkeypatch.setattr(main_module.settings, "invidious_fallback", False)
    monkeypatch.setattr(main_module.settings, "sponsorblock_enabled", False)
    monkeypatch.setattr(main_module.settings, "prefetch_ahead", 0)
    monkeypatch.setattr(main_module.settings, "verify_streams", False)
    main_module.engine.queue.state_path = tmp_path / "queue.json"
    main_module.app.state.open_player = False
    with TestClient(main_module.app) as test_client:
        test_client.headers.update({"X-App-Token": main_module.APP_TOKEN})
        yield test_client
    main_module.engine.queue.clear()


def test_health_needs_no_token(client):
    response = client.get("/health", headers={"X-App-Token": ""})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_api_rejects_missing_token(client):
    assert client.get("/api/state", headers={"X-App-Token": ""}).status_code == 401


def test_api_rejects_wrong_token(client):
    assert client.get("/api/state", headers={"X-App-Token": "nope"}).status_code == 401


def test_search_returns_results(client):
    response = client.get("/api/search", params={"q": "test", "limit": 3})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 3
    assert body[0]["id"] == "vid000"


def test_search_rejects_empty_query(client):
    assert client.get("/api/search", params={"q": ""}).status_code == 422


def test_search_failure_maps_to_502(client, monkeypatch):
    monkeypatch.setenv("STUB_FAIL_MODE", "network")
    response = client.get("/api/search", params={"q": "test"})
    assert response.status_code == 502
    assert "Network" in response.json()["detail"]


def test_queue_add_and_state(client):
    assert client.post("/api/queue", json={"video_id": "abc", "title": "A"}).status_code == 200
    state = client.get("/api/state").json()
    assert [item["video_id"] for item in state["items"]] == ["abc"]


def test_queue_remove(client):
    client.post("/api/queue", json={"video_id": "abc", "title": "A"})
    assert client.delete("/api/queue/0").status_code == 200
    assert client.get("/api/state").json()["items"] == []


def test_queue_remove_out_of_range_is_404(client):
    assert client.delete("/api/queue/7").status_code == 404


def test_queue_reorder_accepts_from_and_to_aliases(client):
    for video_id in ("a", "b", "c"):
        client.post("/api/queue", json={"video_id": video_id, "title": video_id})
    assert client.post("/api/queue/reorder", json={"from": 2, "to": 0}).status_code == 200
    state = client.get("/api/state").json()
    assert [item["video_id"] for item in state["items"]] == ["c", "a", "b"]


def test_queue_reorder_out_of_range_is_400(client):
    client.post("/api/queue", json={"video_id": "a", "title": "a"})
    assert client.post("/api/queue/reorder", json={"from": 0, "to": 9}).status_code == 400


def test_queue_clear(client):
    client.post("/api/queue", json={"video_id": "a", "title": "a"})
    assert client.post("/api/queue/clear").status_code == 200
    assert client.get("/api/state").json()["items"] == []


def test_unknown_player_action_is_404(client):
    assert client.post("/api/player/explode").status_code == 404


def test_player_actions_are_accepted(client):
    client.post("/api/queue", json={"video_id": "a", "title": "a"})
    for action in ("pause", "play", "toggle", "next", "previous", "stop"):
        assert client.post(f"/api/player/{action}").status_code == 200, action


def test_seek_is_not_shadowed_by_the_action_route(client):
    assert client.post("/api/player/seek", json={"seconds": 30}).status_code == 200


def test_volume_is_range_checked(client):
    assert client.post("/api/player/volume", json={"level": 50}).status_code == 200
    assert client.post("/api/player/volume", json={"level": 999}).status_code == 422


def test_settings_round_trip(client):
    updated = client.put("/api/settings", json={"max_height": 720}).json()
    assert updated["max_height"] == 720
    assert client.get("/api/settings").json()["max_height"] == 720


def test_settings_ignores_unknown_keys(client):
    body = client.put("/api/settings", json={"totally_made_up": 1}).json()
    assert "totally_made_up" not in body


def test_settings_accepts_a_valid_ytdlp_channel(client):
    body = client.put("/api/settings", json={"ytdlp_channel": "nightly"}).json()
    assert body["ytdlp_channel"] == "nightly"


def test_settings_rejects_an_invalid_ytdlp_channel(client):
    """The panel only offers valid choices, but a bad value must be a clear 400
    rather than silently poisoning settings.json."""
    response = client.put("/api/settings", json={"ytdlp_channel": "bogus"})
    assert response.status_code == 400
    # And the bad value must not have been written.
    assert client.get("/api/settings").json()["ytdlp_channel"] != "bogus"


def test_settings_accepts_a_valid_overlay_font_size(client):
    body = client.put("/api/settings", json={"overlay_font_size": 40}).json()
    assert body["overlay_font_size"] == 40
    assert client.get("/api/settings").json()["overlay_font_size"] == 40


def test_settings_rejects_an_out_of_range_overlay_font_size(client):
    """The panel bounds this with a spinner, but a value outside the range must
    be a clear 400 rather than a screen-filling overlay."""
    response = client.put("/api/settings", json={"overlay_font_size": 500})
    assert response.status_code == 400
    assert client.get("/api/settings").json()["overlay_font_size"] != 500


def test_settings_accepts_a_valid_overlay_position(client):
    body = client.put("/api/settings", json={"overlay_position": "bottom-left"}).json()
    assert body["overlay_position"] == "bottom-left"
    assert client.get("/api/settings").json()["overlay_position"] == "bottom-left"


def test_settings_rejects_an_invalid_overlay_position(client):
    """The panel only offers real corners, but a bogus value must be a clear 400
    rather than an overlay that never draws."""
    response = client.put("/api/settings", json={"overlay_position": "middle-of-nowhere"})
    assert response.status_code == 400
    assert client.get("/api/settings").json()["overlay_position"] != "middle-of-nowhere"


def test_diagnostics_reports_reliability_state(client):
    """The settings panel surfaces these; docs/reliability.md's first-check
    list must stay reachable in-app."""
    body = client.get("/api/diagnostics").json()
    for key in ("ytdlp_version", "js_runtime", "last_successful_tier", "tier_health", "potoken"):
        assert key in body


def test_potoken_accepts_a_bundle(client):
    response = client.post(
        "/api/potoken",
        json={"visitor_data": "VD", "gvs_token": "GVS", "source": "electron"},
    )
    assert response.json()["ok"] is True
    assert client.get("/api/potoken/status").json()["have_token"] is True


def test_potoken_records_errors(client):
    assert client.post("/api/potoken", json={"error": "BotGuard blew up"}).json()["ok"] is False
    assert client.get("/api/potoken/status").json()["last_error"] == "BotGuard blew up"


def test_websocket_requires_the_token(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?token=wrong"):
            pass


def test_websocket_pushes_initial_state(client):
    client.post("/api/queue", json={"video_id": "abc", "title": "A"})
    with client.websocket_connect(f"/ws?token={main_module.APP_TOKEN}") as websocket:
        payload = websocket.receive_json()
    assert [item["video_id"] for item in payload["items"]] == ["abc"]


def test_diagnostics_reports_environment(client):
    body = client.get("/api/diagnostics").json()
    assert "ytdlp_version" in body
    assert "potoken" in body


def test_shutdown_asks_the_server_to_exit(client, monkeypatch):
    """Windows has no SIGTERM.

    Without a graceful stop the shell can only hard-kill the backend, which
    skips the lifespan teardown and orphans the mpv child process.
    """

    class FakeServer:
        should_exit = False

    server = FakeServer()
    monkeypatch.setattr(main_module.app.state, "server", server, raising=False)

    assert client.post("/api/shutdown").json() == {"ok": True}
    assert server.should_exit is True


def test_shutdown_without_a_server_is_not_an_error(client, monkeypatch):
    monkeypatch.setattr(main_module.app.state, "server", None, raising=False)
    body = client.post("/api/shutdown").json()
    assert body["ok"] is False


def test_shutdown_requires_the_token(client):
    assert client.post("/api/shutdown", headers={"X-App-Token": "nope"}).status_code == 401


@pytest.mark.parametrize("action", ["play", "pause", "toggle", "stop", "open"])
def test_player_actions_report_a_missing_player_cleanly(client, monkeypatch, action):
    """These controls reach straight into mpv.

    With no player they raise from inside the route, and an uncaught
    PlayerUnavailable becomes a bare HTTP 500 -- the UI then shows "HTTP 500"
    instead of saying mpv is missing. (`next` and `previous` are not in this
    list: they go through _play_current, which already catches it and reports
    the problem as a notice instead.)
    """
    from backend.player import PlayerUnavailable

    def unavailable(*args, **kwargs):
        raise PlayerUnavailable("mpv was not found. Install mpv, or set mpv_path in settings.json.")

    for name in ("set_paused", "toggle_pause", "stop", "ensure_started", "load"):
        monkeypatch.setattr(main_module.engine.player, name, unavailable)

    response = client.post(f"/api/player/{action}")
    assert response.status_code == 503, f"{action} returned {response.status_code}"
    body = response.json()
    assert "mpv" in body["detail"]
    assert body["code"] == "notice.player_unavailable"


def test_seek_and_volume_also_report_a_missing_player(client, monkeypatch):
    from backend.player import PlayerUnavailable

    def unavailable(*args, **kwargs):
        raise PlayerUnavailable("the player window is not open")

    monkeypatch.setattr(main_module.engine.player, "seek", unavailable)
    monkeypatch.setattr(main_module.engine.player, "set_volume", unavailable)

    assert client.post("/api/player/seek", json={"seconds": 5}).status_code == 503
    assert client.post("/api/player/volume", json={"level": 50}).status_code == 503


def test_library_endpoints_round_trip(client):
    assert client.put("/api/library/favourites/vid001", json={"title": "Fav Song"}).status_code == 200
    favourites = client.get("/api/library/favourites").json()
    assert [f["id"] for f in favourites] == ["vid001"]
    assert "vid001" in client.get("/api/state").json()["favourites"]
    assert client.get("/api/library/search", params={"q": "fav"}).json()[0]["id"] == "vid001"
    assert client.delete("/api/library/favourites/vid001").status_code == 200
    assert client.get("/api/library/favourites").json() == []
    stats = client.get("/api/library/stats").json()
    assert {"plays", "listened_seconds", "top_tracks", "days"} <= set(stats)
    assert isinstance(client.get("/api/library/history").json(), list)
    assert isinstance(client.get("/api/library/downloads").json(), list)


def test_singer_can_be_set_on_a_queue_item(client):
    client.post("/api/queue", json={"video_id": "abc", "title": "T", "singer": "Maria"})
    assert client.get("/api/state").json()["items"][0]["singer"] == "Maria"
    assert client.put("/api/queue/0/singer", json={"singer": "Eleni"}).status_code == 200
    assert client.get("/api/state").json()["items"][0]["singer"] == "Eleni"
    assert client.put("/api/queue/9/singer", json={"singer": "x"}).status_code == 404


def test_overlay_settings_are_validated(client):
    assert client.put("/api/settings", json={"overlay_style": "bar"}).status_code == 200
    assert client.put("/api/settings", json={"overlay_style": "nope"}).status_code == 400
    assert client.put("/api/settings", json={"overlay_label": "singer"}).status_code == 200
    assert client.put("/api/settings", json={"overlay_boxes": 9}).status_code == 400
    assert client.put("/api/settings", json={"overlay_accent_color": "#12ab34"}).status_code == 200
    assert client.put("/api/settings", json={"overlay_accent_color": "red}{\\fs90"}).status_code == 400


def test_suggest_with_empty_query_is_empty(client):
    assert client.get("/api/suggest", params={"q": " "}).json() == []


def test_logo_upload_is_validated_and_stored(client):
    from backend.player import logo_path

    assert client.post("/api/overlay/logo?w=2&h=2", content=b"\x00" * 15).status_code == 400
    assert client.post("/api/overlay/logo?w=4000&h=2", content=b"\x00" * 32000).status_code == 422
    response = client.post("/api/overlay/logo?w=2&h=2", content=b"\xff" * 16)
    assert response.status_code == 200
    assert main_module.settings.overlay_logo_size == [2, 2]
    assert logo_path().read_bytes() == b"\xff" * 16
    assert client.delete("/api/overlay/logo").status_code == 200
    assert main_module.settings.overlay_logo_size is None
    assert not logo_path().exists()


def test_saved_listing_reports_folder_and_ffmpeg(client):
    body = client.get("/api/library/saved").json()
    assert {"directory", "ffmpeg", "items"} <= set(body)
    assert client.put("/api/settings", json={"download_format": "flac"}).status_code == 400
    assert client.delete("/api/library/saved/nothing").status_code == 409


def test_library_folders_are_listed_and_queueable(client, tmp_path):
    folder = tmp_path / "Karaoke"
    folder.mkdir()
    (folder / "Local Song.mp4").write_bytes(b"v")
    assert client.put("/api/settings", json={"library_folders": [str(folder)]}).status_code == 200
    body = client.get("/api/library/local", params={"q": "local"}).json()
    assert str(folder) in body["folders"]
    [entry] = body["items"]
    assert entry["title"] == "Local Song" and entry["local"]
    assert client.post("/api/queue", json={"video_id": entry["id"]}).status_code == 200
    assert client.get("/api/state").json()["items"][0]["local_path"] == entry["path"]
    assert client.post("/api/queue", json={"video_id": "local-ffffffffffffffff"}).status_code == 404
    assert client.put("/api/settings", json={"library_folders": "C:/"}).status_code == 400
    found = client.get("/api/library/search", params={"q": "local song"}).json()
    assert found[0]["id"] == entry["id"]
    client.put("/api/settings", json={"library_folders": []})


def test_track_stats_endpoint(client):
    main_module.engine.library.remember("statsvid", "Stats Song")
    play = main_module.engine.library.start_play("statsvid", "Maria")
    main_module.engine.library.finish_play(play, 120, True)
    body = client.get("/api/library/track/statsvid").json()
    assert body["plays"] >= 1 and body["singers"][0]["singer"] == "Maria"
    assert client.get("/api/library/track/nothing-here").status_code == 404
