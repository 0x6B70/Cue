"""Library folders: scanning, ids, search and YouTube-copy detection (backend/localfiles.py)."""

from __future__ import annotations

import pytest

from backend.localfiles import LocalLibrary, describe, file_id, is_local_id


@pytest.fixture
def folders(settings, tmp_path):
    downloads = tmp_path / "Videos" / "Cue"
    mine = tmp_path / "Karaoke"
    (downloads / ".cue-partial").mkdir(parents=True)
    (mine / "Greek" / "deeper").mkdir(parents=True)
    (downloads / "Get Lucky [5NV6Rdv1a3I].mkv").write_bytes(b"v")
    (downloads / ".cue-partial" / "video").write_bytes(b"half")
    (mine / "Μαρίζα Ρίζου - Είσαι εδώ.mp4").write_bytes(b"vv")
    (mine / "Greek" / "deeper" / "Song.mp3").write_bytes(b"a")
    (mine / "notes.txt").write_text("not media")
    (mine / ".hidden.mp4").write_bytes(b"h")
    settings.download_dir = str(downloads)
    settings.library_folders = [str(mine)]
    return LocalLibrary(settings), downloads, mine


def test_scan_lists_media_in_every_folder_and_skips_the_rest(folders):
    library, downloads, mine = folders
    names = sorted(f.path.name for f in library.scan())
    assert names == ["Get Lucky [5NV6Rdv1a3I].mkv", "Song.mp3", "Μαρίζα Ρίζου - Είσαι εδώ.mp4"]


def test_the_download_folder_is_always_included_once(folders, settings):
    library, downloads, _ = folders
    settings.library_folders = [str(downloads), str(downloads)]
    assert library.folders() == [downloads]


def test_ids_are_stable_and_marked_local(folders):
    library, _, mine = folders
    path = mine / "Μαρίζα Ρίζου - Είσαι εδώ.mp4"
    assert file_id(path) == file_id(path)
    assert is_local_id(file_id(path)) and not is_local_id("5NV6Rdv1a3I")
    assert library.find(file_id(path)).path == path


def test_a_saved_youtube_file_is_recognised(folders):
    library, downloads, _ = folders
    assert describe(downloads / "Get Lucky [5NV6Rdv1a3I].mkv") == ("Get Lucky", "5NV6Rdv1a3I")
    assert library.copy_of("5NV6Rdv1a3I") == downloads / "Get Lucky [5NV6Rdv1a3I].mkv"
    entry = next(f for f in library.scan() if f.youtube_id).public()
    assert entry["id"] == "5NV6Rdv1a3I"  # queued as the YouTube video itself


def test_search_is_accent_and_case_insensitive(folders):
    library, _, _ = folders
    assert [f.title for f in library.search("μαριζα ριζου")] == ["Μαρίζα Ρίζου - Είσαι εδώ"]
    assert [f.title for f in library.search("greek")] == []  # parent is "deeper", not "Greek"
    assert [f.title for f in library.search("deeper")] == ["Song"]


def test_suggest_completes_file_titles(folders):
    library, _, _ = folders
    assert library.suggest("μαρ") == ["Μαρίζα Ρίζου - Είσαι εδώ"]
    assert library.suggest("lucky") == ["Get Lucky"]


def test_a_new_file_is_found_after_a_rescan(folders):
    library, _, mine = folders
    library.scan()
    new = mine / "New one.webm"
    new.write_bytes(b"n")
    assert library.find(file_id(new)).path == new  # a miss forces a rescan


def test_contains_rejects_paths_outside_the_folders(folders, tmp_path):
    library, _, mine = folders
    assert library.contains(mine / "Greek" / "deeper" / "Song.mp3")
    assert not library.contains(tmp_path / "elsewhere.mp4")
