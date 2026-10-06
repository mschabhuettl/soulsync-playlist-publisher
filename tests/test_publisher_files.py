import os
from pathlib import Path
import time

import pytest

import publisher_files as files


@pytest.fixture
def library(tmp_path):
    roots = {name: tmp_path / name for name in ("Alice", "Bob", "Shared", "Transfer")}
    for root in roots.values():
        root.mkdir()
    profile = {"read_roots": [str(roots["Alice"]), str(roots["Shared"])],
               "write_root": str(roots["Alice"])}
    return roots, profile, list(map(str, roots.values()))


def track(path, **kwargs):
    return {"key": "spotify:1", "title": "Song", "artist": "Artist", "album": "Album",
            "source_paths": [str(path)], **kwargs}


def song(path, **kwargs):
    return {"path": str(path), "title": "Song", "artist": "Artist", "album": "Album", **kwargs}


def audio(root, name="Song.flac", content=b"audio content"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_foreign_copy_is_independent_and_dry_run_never_mutates(library):
    roots, profile, allowed = library
    original = audio(roots["Bob"])
    before = sorted(str(p) for p in roots["Alice"].rglob("*"))
    plan = files.plan_track(track(original), profile, allowed, {})
    assert plan["status"] == "copy"
    assert sorted(str(p) for p in roots["Alice"].rglob("*")) == before
    target = Path(files.install_copy(plan))
    assert target.is_relative_to(roots["Alice"] / "_SoulSync")
    assert target.read_bytes() == original.read_bytes()
    assert target.stat().st_ino != original.stat().st_ino
    target.write_bytes(b"personal edit")
    assert original.read_bytes() == b"audio content"
    assert not list(roots["Alice"].rglob("*.part"))


def test_both_reuse_needs_account_specific_inventory_id(library):
    roots, profile, allowed = library
    shared = audio(roots["Shared"])
    plan = files.plan_track(track(shared), profile, allowed, {"shared-id": song(shared)})
    assert plan["status"] == "ready"
    assert plan["song_id"] == "shared-id"
    assert plan["target"] == str(shared)
    assert not list(roots["Alice"].iterdir())
    unindexed = files.plan_track(track(shared), profile, allowed, {})
    assert unindexed["status"] == "missing"
    assert unindexed["target"] == str(shared)


def test_personal_metadata_match_reuses_copy_when_global_match_is_foreign(library):
    roots, profile, allowed = library
    foreign = audio(roots["Bob"])
    own = audio(roots["Alice"], "Imported.flac")
    plan = files.plan_track(track(foreign), profile, allowed, {"personal-id": song(own)})
    assert plan["status"] == "ready"
    assert plan["song_id"] == "personal-id"


def test_exact_accessible_path_prefers_personal_over_both(library):
    roots, profile, allowed = library
    own = audio(roots["Alice"])
    shared = audio(roots["Shared"])
    item = track(own, source_paths=[str(shared), str(own)])
    plan = files.plan_track(item, profile, allowed, {"shared": song(shared), "own": song(own)})
    assert plan["song_id"] == "own"


def test_metadata_album_mismatch_is_not_reused(library):
    roots, profile, allowed = library
    foreign = audio(roots["Bob"])
    own = audio(roots["Alice"])
    plan = files.plan_track(track(foreign), profile, allowed, {"different": song(own, album="Live")})
    assert plan["status"] == "copy"


@pytest.mark.parametrize("seconds", [60, 0, None])
def test_metadata_short_edit_or_unknown_duration_is_not_reused(library, seconds):
    roots, profile, allowed = library
    foreign = audio(roots["Bob"])
    own = audio(roots["Alice"])
    plan = files.plan_track(track(foreign, duration_ms=180000), profile, allowed,
                            {"edit": song(own, duration=seconds)})
    assert plan["status"] == "copy"


def test_metadata_near_equal_duration_can_reuse_personal_copy(library):
    roots, profile, allowed = library
    foreign = audio(roots["Bob"])
    own = audio(roots["Alice"])
    plan = files.plan_track(track(foreign, duration_ms=180000), profile, allowed,
                            {"personal": song(own, duration=180.3)})
    assert plan["status"] == "ready"
    assert plan["song_id"] == "personal"


def test_ambiguous_accessible_metadata_ids_are_not_guessed(library):
    roots, profile, allowed = library
    own1 = audio(roots["Alice"], "1.flac")
    own2 = audio(roots["Alice"], "2.flac")
    plan = files.plan_track(track("/missing.flac"), profile, allowed,
                            {"1": song(own1), "2": song(own2)})
    assert plan["status"] == "ambiguous"


def test_source_outside_allowlist_and_symlink_escape_are_refused(library, tmp_path):
    roots, profile, allowed = library
    outside = audio(tmp_path, "Secret.flac")
    linked = roots["Bob"] / "Escaped.flac"
    linked.symlink_to(outside)
    assert files.plan_track(track(outside), profile, allowed, {})["status"] == "missing"
    assert files.plan_track(track(linked), profile, allowed, {})["status"] == "missing"


def test_directory_and_wrong_extension_not_audio(library):
    roots, profile, allowed = library
    text = audio(roots["Bob"], "not-audio.txt")
    assert files.plan_track(track(text), profile, allowed, {})["status"] == "missing"
    assert files.plan_track(track(roots["Bob"]), profile, allowed, {})["status"] == "missing"


def test_destination_symlink_inserted_after_plan_is_refused(library):
    roots, profile, allowed = library
    original = audio(roots["Bob"])
    plan = files.plan_track(track(original), profile, allowed, {})
    (roots["Alice"] / "_SoulSync").symlink_to(roots["Bob"], target_is_directory=True)
    with pytest.raises(files.PublisherFileError, match="escapes"):
        files.install_copy(plan)
    assert sorted(p.name for p in roots["Bob"].iterdir()) == ["Song.flac"]


def test_existing_identical_target_is_reused_without_overwrite(library):
    roots, profile, allowed = library
    original = audio(roots["Bob"])
    plan = files.plan_track(track(original), profile, allowed, {})
    target = Path(files.install_copy(plan))
    before = target.stat()
    assert files.install_copy(plan) == str(target)
    assert target.stat().st_ino == before.st_ino
    assert target.stat().st_mtime_ns == before.st_mtime_ns


def test_existing_different_target_is_never_overwritten(library):
    roots, profile, allowed = library
    original = audio(roots["Bob"])
    plan = files.plan_track(track(original), profile, allowed, {})
    target = Path(plan["target"])
    target.parent.mkdir(parents=True)
    target.write_bytes(b"other original")
    with pytest.raises(files.PublisherFileError, match="collision"):
        files.install_copy(plan)
    assert target.read_bytes() == b"other original"
    assert original.read_bytes() == b"audio content"


def test_changed_source_after_planning_refused(library):
    roots, profile, allowed = library
    original = audio(roots["Bob"])
    plan = files.plan_track(track(original), profile, allowed, {})
    original.write_bytes(b"new content")
    with pytest.raises(files.SourceChangedError):
        files.install_copy(plan)
    assert not Path(plan["target"]).exists()


def test_changed_source_during_copy_refused_and_temporary_cleaned(library, monkeypatch):
    roots, profile, allowed = library
    original = audio(roots["Bob"])
    plan = files.plan_track(track(original), profile, allowed, {})
    calls = 0
    def change_during_copy(_deadline):
        nonlocal calls
        calls += 1
        if calls == 2:
            original.write_bytes(b"changing source")
    monkeypatch.setattr(files, "_check_deadline", change_during_copy)
    with pytest.raises(files.SourceChangedError):
        files.install_copy(plan)
    assert not Path(plan["target"]).exists()
    assert not list(roots["Alice"].rglob("*.part"))
    assert original.read_bytes() == b"changing source"


def test_deadline_prevents_any_copy_mutation(library):
    roots, profile, allowed = library
    original = audio(roots["Bob"])
    plan = files.plan_track(track(original), profile, allowed, {})
    with pytest.raises(files.PublisherFileError, match="deadline"):
        files.install_copy(plan, deadline=time.monotonic() - 1)
    assert not list(roots["Alice"].iterdir())


def test_different_global_sources_are_ambiguous(library):
    roots, profile, allowed = library
    first = audio(roots["Bob"])
    second = audio(roots["Transfer"], content=b"another recording")
    plan = files.plan_track(track(first, source_paths=[str(first), str(second)]), profile, allowed, {})
    assert plan["status"] == "ambiguous"


def test_exclusive_publication_does_not_overwrite_racing_target(library, monkeypatch):
    roots, profile, allowed = library
    original = audio(roots["Bob"])
    plan = files.plan_track(track(original), profile, allowed, {})
    real_publish = files._publish_exclusive
    def race(staged, target):
        Path(target).write_bytes(b"racing original")
        real_publish(staged, target)
    monkeypatch.setattr(files, "_publish_exclusive", race)
    with pytest.raises(files.PublisherFileError, match="collision"):
        files.install_copy(plan)
    assert Path(plan["target"]).read_bytes() == b"racing original"
    assert not list(roots["Alice"].rglob("*.part"))


def test_download_titles_cannot_escape_destination(library):
    roots, profile, allowed = library
    original = audio(roots["Bob"])
    plan = files.plan_track(track(original, title="../../Song", artist="../Other", album="/outside"), profile, allowed, {})
    assert Path(plan["target"]).is_relative_to(roots["Alice"] / "_SoulSync")
    assert ".." not in Path(plan["target"]).parts


def test_prepared_inventory_does_not_recheck_entire_library_per_track(library, monkeypatch):
    roots, profile, allowed = library
    inventory = {str(i): song(audio(roots["Alice"], f"{i}.flac"), title=f"Song {i}")
                 for i in range(30)}
    prepared = files.prepare_inventory(inventory, profile)
    real_check = files._allowed_path
    checked = []
    def count_checks(path, *args, **kwargs):
        checked.append(str(path))
        return real_check(path, *args, **kwargs)
    monkeypatch.setattr(files, "_allowed_path", count_checks)
    for i in range(5):
        plan = files.plan_track(track("/missing.flac", title=f"Song {i}"), profile, allowed, prepared)
        assert plan["song_id"] == str(i)
    # One requested source check and one chosen file check per track. None of
    # the other 29 inventory entries is walked again for each request.
    assert len(checked) == 10
    assert len(prepared) == 30
    with pytest.raises(files.PublisherFileError, match="different"):
        files.plan_track(track("/missing.flac"),
                         {"read_roots": [str(roots["Bob"])], "write_root": str(roots["Bob"])},
                         allowed, prepared)


def test_planning_hash_respects_deadline(library, monkeypatch):
    roots, profile, allowed = library
    original = audio(roots["Bob"])
    ticks = iter([0, 20])  # plan begins in time; first hash read exceeds budget
    monkeypatch.setattr(files.time, "monotonic", lambda: next(ticks))
    with pytest.raises(files.PublisherFileError, match="deadline"):
        files.plan_track(track(original), profile, allowed, {}, deadline=10)
    assert not list(roots["Alice"].iterdir())
