from __future__ import annotations

import time

from backend.potoken import PoTokenBundle, PoTokenStore


def test_bundle_requires_visitor_data_and_a_token():
    assert not PoTokenBundle(visitor_data="", gvs_token="G").valid
    assert not PoTokenBundle(visitor_data="VD").valid
    assert PoTokenBundle(visitor_data="VD", gvs_token="G").valid


def test_extractor_args_render_yt_dlp_syntax():
    bundle = PoTokenBundle(visitor_data="VD", gvs_token="GVS", player_token="PLY")
    flag, value = bundle.extractor_args("web")
    assert flag == "--extractor-args"
    assert value.startswith("youtube:")
    parts = value.removeprefix("youtube:").split(";")
    assert "player_client=web" in parts
    assert "visitor_data=VD" in parts
    assert "po_token=web.gvs+GVS,web.player+PLY" in parts


def test_extractor_args_omit_missing_token_types():
    value = PoTokenBundle(visitor_data="VD", gvs_token="GVS").extractor_args("web")[1]
    assert "web.gvs+GVS" in value
    assert "player+" not in value


def test_extractor_args_follow_the_requested_client():
    value = PoTokenBundle(visitor_data="VD", gvs_token="G").extractor_args("mweb")[1]
    assert "player_client=mweb" in value
    assert "mweb.gvs+G" in value


def test_store_rejects_incomplete_bundles():
    store = PoTokenStore()
    store.set(PoTokenBundle(visitor_data="VD"))
    assert store.get() is None


def test_store_returns_fresh_bundle():
    store = PoTokenStore()
    store.set(PoTokenBundle(visitor_data="VD", gvs_token="G"))
    assert store.get() is not None
    assert store.status["have_token"] is True


def test_store_treats_near_expiry_as_stale():
    """Refreshing early keeps a resolve from racing the expiry."""
    store = PoTokenStore()
    store.set(PoTokenBundle(visitor_data="VD", gvs_token="G", expires_at=time.time() + 60))
    assert store.get() is None
    assert store.status["have_token"] is False


def test_store_records_errors():
    store = PoTokenStore()
    store.set_error("BotGuard failed")
    assert store.status["last_error"] == "BotGuard failed"


def test_successful_set_clears_previous_error():
    store = PoTokenStore()
    store.set_error("transient")
    store.set(PoTokenBundle(visitor_data="VD", gvs_token="G"))
    assert store.status["last_error"] is None
