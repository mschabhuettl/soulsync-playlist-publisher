"""Transport-level fixtures: never contact an actual Navidrome server."""

import copy
import hashlib
from urllib.parse import urlsplit

import pytest
import requests

from navidrome_api import NavClient, NavidromeError, absolute_song_path


MARKER = "soulsync-profile-publisher/v1 profile=2 playlist=7"
NAME = "SoulSync · Road Trip"


class Response:
    def __init__(self, result=None, *, status_code=200):
        self.status_code = status_code
        self.result = {"subsonic-response": {"status": "ok", **(result or {})}}

    def json(self):
        return copy.deepcopy(self.result)


class FakeSession:
    """Tiny API server with personal inventories and preserved repeated params."""
    def __init__(self):
        self.calls = []
        self.songs = {
            "alice": [{"id": "m1", "path": "/music/Alice/one.flac"},
                    {"id": "both", "path": "/music/Shared/shared.flac"}],
            "bob": [{"id": "n1", "path": "/music/Bob/one.flac"},
                    {"id": "both", "path": "/music/Shared/shared.flac"}],
        }
        self.playlists = {}
        self.hooks = {}
        self.scan_states = []
        self.page_cap = 1  # Deliberately less than the requested page size.

    def playlist(self, pid="mine", *, owner="alice", comment=MARKER, name=NAME, public=False, ids=()):
        self.playlists[pid] = {
            "id": pid, "owner": owner, "comment": comment, "name": name,
            "public": public, "entry": [{"id": sid} for sid in ids], "songCount": len(ids),
        }
        return self.playlists[pid]

    def post(self, url, *, data, timeout, verify, allow_redirects):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append((endpoint, list(data), url, timeout, verify, allow_redirects))
        params = dict(data)
        if endpoint in self.hooks:
            override = self.hooks[endpoint](params, data)
            if override is not None:
                return override
        if endpoint == "ping":
            return Response()
        if endpoint in ("getScanStatus", "startScan"):
            state = self.scan_states.pop(0) if self.scan_states else {"count": 3, "scanning": False}
            return Response({"scanStatus": state})
        if endpoint == "search3":
            start = int(params["songOffset"])
            songs = self.songs[params["u"]][start:start + self.page_cap]
            return Response({"searchResult3": {"song": songs}})
        if endpoint == "getPlaylists":
            return Response({"playlists": {"playlist": list(self.playlists.values())}})
        if endpoint == "getPlaylist":
            return Response({"playlist": self.playlists[params["id"]]})
        if endpoint == "createPlaylist":
            pid = params.get("playlistId")
            if pid is None:
                pid = f"created-{len(self.playlists)}"
                playlist = self.playlist(pid, owner=params["u"], comment="", name=params["name"])
            else:
                playlist = self.playlists[pid]
            ids = [value for key, value in data if key == "songId"]
            playlist["entry"] = [{"id": sid} for sid in ids]
            playlist["songCount"] = len(ids)
            playlist["comment"] = ""  # Exercise metadata-reset compatibility.
            return Response({"playlist": playlist})
        if endpoint == "updatePlaylist":
            playlist = self.playlists[params["playlistId"]]
            playlist.update(name=params["name"], comment=params["comment"], public=params["public"] == "true")
            return Response()
        raise AssertionError(f"Unexpected endpoint {endpoint}")

    @property
    def mutations(self):
        return [call for call in self.calls if call[0] in ("createPlaylist", "updatePlaylist", "startScan")]


@pytest.fixture
def setup():
    session = FakeSession()
    client = NavClient("https://nav.example/base", "alice", "password", session=session)
    return client, session


def test_authentication_is_salted_and_only_in_post_body(setup):
    client, session = setup
    assert client.connect() is True
    client.connect()
    salts = []
    for endpoint, data, url, timeout, verify, redirects in session.calls:
        form = dict(data)
        assert endpoint == "ping" and urlsplit(url).query == ""
        assert "password" not in url and "alice" not in url
        assert "p" not in form and form["u"] == "alice" and form["c"] == "SoulSync"
        assert form["t"] == hashlib.md5(("password" + form["s"]).encode()).hexdigest()
        assert timeout == (5.0, 15.0) and verify is True and redirects is False
        salts.append(form["s"])
    assert salts[0] != salts[1]


@pytest.mark.parametrize("url", ["ftp://nav.example", "https://u:secret@nav.example", "https://nav.example?p=secret", "https://nav.example/#secret"])
def test_unsafe_base_url_rejected(url):
    with pytest.raises(ValueError):
        NavClient(url, "alice", "password")


def test_inventory_uses_personal_visibility_and_pages_past_short_page(setup):
    client, session = setup
    assert set(client.inventory()) == {"m1", "both"}
    searches = [dict(call[1]) for call in session.calls if call[0] == "search3"]
    assert [p["songOffset"] for p in searches] == [0, 1, 2]
    assert all(p["u"] == "alice" and p["artistCount"] == 0 and p["albumCount"] == 0 for p in searches)
    assert [c[0] for c in session.calls].count("getScanStatus") == 2
    second = NavClient(client.url, "bob", "other-password", session=session)
    assert set(second.inventory()) == {"n1", "both"}


@pytest.mark.parametrize("result", [{}, {"searchResult3": None}, {"searchResult3": {"song": {}}},
                                      {"searchResult3": {"song": [None]}},
                                      {"searchResult3": {"song": [{}]}}])
def test_malformed_inventory_is_not_treated_as_empty(setup, result):
    client, session = setup
    session.hooks["search3"] = lambda *_: Response(result)
    with pytest.raises(NavidromeError):
        client.inventory()


def test_repeated_inventory_page_rejected(setup):
    client, session = setup
    session.hooks["search3"] = lambda *_: Response({"searchResult3": {"song": session.songs["alice"][:1]}})
    with pytest.raises(NavidromeError, match="repeated"):
        client.inventory()


@pytest.mark.parametrize("states", [
    [{"count": 3, "scanning": True}],
    [{"count": 3, "scanning": False}, {"count": 4, "scanning": False}],
    [{"count": 3, "scanning": False}, {"count": 3, "scanning": True}],
    [{"count": 3, "scanning": False, "lastScan": "before"}, {"count": 3, "scanning": False, "lastScan": "after"}],
    [{"count": 3}],
    [{"scanning": False}],
])
def test_active_or_changed_or_unknown_scan_refuses_inventory(setup, states):
    client, session = setup
    session.scan_states = states
    with pytest.raises(NavidromeError):
        client.inventory()


def test_inventory_limits_are_enforced(setup):
    client, session = setup
    client.max_songs = 1
    with pytest.raises(NavidromeError, match="song limit"):
        client.inventory()
    session.calls.clear()
    with pytest.raises(NavidromeError, match="time limit"):
        client.inventory(deadline=0)
    assert session.calls == []


def test_empty_personal_library_can_be_valid_while_global_count_is_nonzero(setup):
    client, session = setup
    session.songs["alice"] = []
    assert client.inventory() == {}


def test_publish_rejects_foreign_id_without_mutation(setup):
    client, session = setup
    with pytest.raises(NavidromeError, match="not visible"):
        client.publish_playlist(NAME, ["n1"], marker=MARKER)
    assert session.mutations == []


@pytest.mark.parametrize("path", [None, "Artist/Song.flac", "", "/", "/music/\x00bad"])
def test_selected_track_requires_real_absolute_path(setup, path):
    client, session = setup
    session.songs["alice"][0]["path"] = path
    with pytest.raises(NavidromeError, match="path"):
        client.publish_playlist(NAME, ["m1"], marker=MARKER)
    assert session.mutations == []


def test_create_is_private_before_tracks_and_preserves_order_and_duplicates(setup):
    client, session = setup
    pid = client.publish_playlist(NAME, ["both", "m1", "both"], marker=MARKER)
    playlist = session.playlists[pid]
    assert playlist["owner"] == "alice" and playlist["comment"] == MARKER and playlist["public"] is False
    assert [song["id"] for song in playlist["entry"]] == ["both", "m1", "both"]
    writes = session.mutations
    assert writes[0][0] == "createPlaylist" and not any(k == "songId" for k, _ in writes[0][1])
    assert writes[1][0] == "updatePlaylist" and dict(writes[1][1])["public"] == "false"
    assert writes[2][0] == "createPlaylist"
    assert [v for k, v in writes[2][1] if k == "songId"] == ["both", "m1", "both"]


def test_existing_marked_private_playlist_is_updated_in_place(setup):
    client, session = setup
    session.playlist(ids=["m1"])
    assert client.publish_playlist(NAME, ["both", "both"], state_playlist_id="mine", marker=MARKER) == "mine"
    assert list(session.playlists) == ["mine"]


def test_unchanged_marked_private_playlist_is_verified_without_writes(setup):
    client, session = setup
    session.playlist(ids=["both", "m1", "both"])
    assert client.publish_playlist(
        NAME, ["both", "m1", "both"], state_playlist_id="mine", marker=MARKER,
        expected_paths={"m1": "/music/Alice/one.flac", "both": "/music/Shared/shared.flac"},
    ) == "mine"
    assert session.mutations == []
    assert any(call[0] == "search3" for call in session.calls)
    assert [call[0] for call in session.calls].count("getPlaylist") == 1


def test_unchanged_playlist_does_not_bypass_fresh_expected_path_validation(setup):
    client, session = setup
    session.playlist(ids=["m1"])
    session.songs["alice"][0]["path"] = "/music/Bob/one.flac"
    with pytest.raises(NavidromeError, match="path changed"):
        client.publish_playlist(NAME, ["m1"], state_playlist_id="mine", marker=MARKER,
                                expected_paths={"m1": "/music/Alice/one.flac"})
    assert session.mutations == []


def test_marker_recovers_publisher_owned_playlist_when_state_is_missing(setup):
    client, session = setup
    session.playlist(name="Old source title")
    assert client.publish_playlist(NAME, ["m1"], marker=MARKER) == "mine"
    assert session.playlists["mine"]["name"] == NAME


@pytest.mark.parametrize("owner,comment", [("bob", MARKER), ("alice", ""), ("alice", "someone-else"), (None, MARKER)])
def test_name_collision_never_adopts_foreign_or_unmarked_playlist(setup, owner, comment):
    client, session = setup
    session.playlist(owner=owner, comment=comment)
    before = copy.deepcopy(session.playlists)
    with pytest.raises(NavidromeError):
        client.publish_playlist(NAME, ["m1"], marker=MARKER)
    assert session.mutations == [] and session.playlists == before


def test_state_id_alone_does_not_authorize_an_unmarked_playlist(setup):
    client, session = setup
    session.playlist(name="Manual playlist", comment="")
    with pytest.raises(NavidromeError, match="unmarked"):
        client.publish_playlist(NAME, ["m1"], state_playlist_id="mine", marker=MARKER)
    assert session.mutations == []


def test_state_id_cannot_authorize_another_users_marked_playlist(setup):
    client, session = setup
    session.playlist(name="Different name", owner="bob")
    with pytest.raises(NavidromeError, match="another account"):
        client.publish_playlist(NAME, ["m1"], state_playlist_id="mine", marker=MARKER)
    assert session.mutations == []


def test_duplicate_marker_is_ambiguous_and_does_not_mutate(setup):
    client, session = setup
    session.playlist("one")
    session.playlist("two", name="Another name")
    with pytest.raises(NavidromeError, match="Ambiguous"):
        client.publish_playlist(NAME, ["m1"], marker=MARKER)
    assert session.mutations == []


def test_empty_playlist_is_rejected_before_network_and_never_clears_existing(setup):
    client, session = setup
    session.playlist(ids=["m1"])
    with pytest.raises(NavidromeError, match="Empty playlists"):
        client.publish_playlist(NAME, [], marker=MARKER)
    assert session.playlists["mine"]["entry"] == [{"id": "m1"}]
    assert session.calls == []


def test_create_without_returned_id_does_not_adopt_by_name(setup):
    client, session = setup
    session.hooks["createPlaylist"] = lambda *_: Response()
    with pytest.raises(NavidromeError, match="verifiable ID"):
        client.publish_playlist(NAME, ["m1"], marker=MARKER)
    assert [c[0] for c in session.mutations] == ["createPlaylist"]


@pytest.mark.parametrize("corruption", ["order", "missing", "public", "owner", "comment", "count"])
def test_publish_never_claims_success_when_readback_differs(setup, corruption):
    client, session = setup
    session.playlist(ids=[])
    reads = 0

    def corrupt(params, _data):
        nonlocal reads
        reads += 1
        if reads == 3:  # Last read after content + metadata updates.
            playlist = copy.deepcopy(session.playlists[params["id"]])
            if corruption == "order":
                playlist["entry"].reverse()
            elif corruption == "missing":
                playlist["entry"].pop()
            elif corruption == "public":
                playlist["public"] = True
            elif corruption == "count":
                playlist["songCount"] += 1
            else:
                playlist[corruption] = "unexpected"
            return Response({"playlist": playlist})

    session.hooks["getPlaylist"] = corrupt
    with pytest.raises(NavidromeError):
        client.publish_playlist(NAME, ["both", "m1"], marker=MARKER)


def test_failed_privacy_confirmation_prevents_song_write(setup):
    client, session = setup
    session.playlist(public=True)
    session.hooks["updatePlaylist"] = lambda *_: Response()  # Pretend success without changing privacy.
    with pytest.raises(NavidromeError, match="privacy"):
        client.publish_playlist(NAME, ["m1"], marker=MARKER)
    assert not any(call[0] == "createPlaylist" for call in session.calls)


@pytest.mark.parametrize("failure", ["network", "http", "redirect", "api", "json"])
def test_failures_are_sanitized_and_never_return_success(setup, failure):
    client, session = setup

    def fail(*_args):
        if failure == "network":
            raise requests.ConnectionError("password secret token and body")
        if failure == "http":
            return Response(status_code=503)
        if failure == "redirect":
            return Response(status_code=307)
        if failure == "api":
            return Response({"status": "failed", "error": {"message": "password secret token"}})
        response = Response()
        response.json = lambda: (_ for _ in ()).throw(ValueError("password secret token"))
        return response

    session.hooks["ping"] = fail
    with pytest.raises(NavidromeError) as error:
        client.connect()
    assert "password" not in str(error.value) and "secret" not in str(error.value)


def test_scan_request_uses_calling_account_without_credential_fallback(setup):
    client, session = setup
    session.hooks["startScan"] = lambda *_: Response({"status": "failed", "error": {"code": 50}})
    with pytest.raises(NavidromeError):
        client.request_scan()
    assert len(session.calls) == 1 and dict(session.calls[0][1])["u"] == "alice"


def test_admin_scan_request_returns_validated_status():
    session = FakeSession()
    session.scan_states = [{"count": 3, "scanning": True}]
    client = NavClient("http://nav.example", "soulsync", "service-password", session=session)
    assert client.request_scan() == {"count": 3, "scanning": True}
    assert dict(session.calls[0][1])["u"] == "soulsync"


def test_failed_song_write_raises_without_claiming_publication(setup):
    client, session = setup
    session.playlist(ids=["m1"])
    session.hooks["createPlaylist"] = lambda *_: Response({"status": "failed"})
    with pytest.raises(NavidromeError, match="createPlaylist"):
        client.publish_playlist(NAME, ["both"], marker=MARKER)
    assert session.playlists["mine"]["entry"] == [{"id": "m1"}]


def test_absolute_song_path_uses_server_path_without_local_resolution():
    assert absolute_song_path({"path": "/music/Alice/Artist/../song.flac"}) == "/music/Alice/song.flac"


def test_same_id_pointing_to_different_visible_path_is_refused_without_mutation(setup):
    client, session = setup
    # Still visible to these credentials, but no longer the own-library file
    # selected by the plan. Visibility alone is insufficient to reuse this ID.
    session.songs["alice"][0]["path"] = "/music/Bob/different.flac"
    with pytest.raises(NavidromeError, match="path changed"):
        client.publish_playlist(NAME, ["m1"], marker=MARKER,
                                expected_paths={"m1": "/music/Alice/one.flac"})
    assert session.mutations == []


def test_expected_paths_accept_normalized_equality_and_keep_duplicates(setup):
    client, session = setup
    pid = client.publish_playlist(NAME, ["m1", "both", "m1"], marker=MARKER, expected_paths={
        "m1": "/music/Alice/./one.flac", "both": "/music/Shared/shared.flac",
    })
    assert [s["id"] for s in session.playlists[pid]["entry"]] == ["m1", "both", "m1"]


@pytest.mark.parametrize("expected", [{}, {"m1": "relative.flac"}, []])
def test_incomplete_expected_paths_fail_before_network(setup, expected):
    client, session = setup
    with pytest.raises(NavidromeError):
        client.publish_playlist(NAME, ["m1"], marker=MARKER, expected_paths=expected)
    assert session.calls == []


def test_publish_expired_deadline_does_not_issue_request(setup):
    client, session = setup
    with pytest.raises(NavidromeError, match="time limit"):
        client.publish_playlist(NAME, ["m1"], marker=MARKER, deadline=0)
    assert session.calls == []


def test_deadline_expiring_after_preflight_prevents_first_write(setup, monkeypatch):
    import navidrome_api
    client, session = setup
    now = [10.0]
    monkeypatch.setattr(navidrome_api.time, "monotonic", lambda: now[0])

    def expire(_params, _data):
        now[0] = 21.0
        return Response({"playlists": {"playlist": []}})

    session.hooks["getPlaylists"] = expire
    with pytest.raises(NavidromeError, match="time limit"):
        client.publish_playlist(NAME, ["m1"], marker=MARKER, deadline=20.0)
    assert session.mutations == []


def test_deadline_expiring_mid_publish_prevents_next_request(setup, monkeypatch):
    import navidrome_api
    client, session = setup
    session.playlist(ids=["m1"])
    now = [10.0]
    monkeypatch.setattr(navidrome_api.time, "monotonic", lambda: now[0])

    def expire(_params, _data):
        now[0] = 21.0
        return Response()

    session.hooks["updatePlaylist"] = expire
    with pytest.raises(NavidromeError, match="time limit"):
        client.publish_playlist(NAME, ["both"], marker=MARKER, deadline=20.0)
    assert session.calls[-1][0] == "updatePlaylist"
    assert [call[0] for call in session.mutations] == ["updatePlaylist"]
    assert session.playlists["mine"]["entry"] == [{"id": "m1"}]


def test_call_timeouts_shrink_with_remaining_deadline(setup, monkeypatch):
    import navidrome_api
    client, session = setup
    monkeypatch.setattr(navidrome_api.time, "monotonic", lambda: 10.0)
    client.connect(deadline=12.0)
    assert session.calls[0][3] == (1.0, 1.0)
