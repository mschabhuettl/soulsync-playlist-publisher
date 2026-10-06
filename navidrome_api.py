"""Small, fail-closed OpenSubsonic adapter for personal Navidrome accounts.

No SoulSync code or database is imported. Authentication is sent only in POST
form bodies; URLs and exceptions never contain credentials or response bodies.
The default client name deliberately matches SoulSync's Navidrome player, whose
Real Path setting must be enabled. Playlist writes require fresh personal song
visibility, an exact ownership marker, and a successful readback.
"""

from __future__ import annotations

import hashlib
import posixpath
import secrets
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import requests


class NavidromeError(RuntimeError):
    """The server could not safely complete or verify the requested operation."""


def absolute_song_path(song: dict[str, Any]) -> str:
    """Return a normalized absolute server path, or reject unavailable Real Path.

    Do not use os.path.realpath: the server's filesystem is not this container's.
    Filesystem authorization and local path mapping belong to the caller.
    """
    path = song.get("path")
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise NavidromeError("Selected Navidrome song has no absolute path; enable Real Path for this account's SoulSync player")
    path = posixpath.normpath(path)
    if path in ("/", "//"):
        raise NavidromeError("Selected Navidrome song has an invalid absolute path")
    return path


class NavClient:
    def __init__(
        self, url: str, username: str, password: str, client_name: str = "SoulSync",
        *, session=None, timeout: tuple[float, float] = (5.0, 15.0),
        page_size: int = 500, inventory_timeout: float = 120.0,
        max_songs: int = 1_000_000,
    ):
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError("Navidrome URL must be an HTTP(S) base URL without credentials, query or fragment")
        if not all(isinstance(value, str) and value for value in (username, password, client_name)):
            raise ValueError("Navidrome username, password and client name are required")
        if page_size < 1 or max_songs < 1 or inventory_timeout <= 0:
            raise ValueError("Inventory limits must be positive")
        if len(timeout) != 2 or any(value <= 0 for value in timeout):
            raise ValueError("Both request timeouts must be positive")
        self.url = url.rstrip("/")
        self.username = username
        self._password = password
        self.client_name = client_name
        self._session = session if session is not None else requests.Session()
        self.timeout = timeout
        self.page_size = page_size
        self.inventory_timeout = inventory_timeout
        self.max_songs = max_songs

    def _call(
        self, endpoint: str, params: Iterable[tuple[str, Any]] = (),
        *, deadline: float | None = None,
    ) -> dict[str, Any]:
        salt = secrets.token_hex(16)
        token = hashlib.md5((self._password + salt).encode("utf-8")).hexdigest()
        form = [("u", self.username), ("t", token), ("s", salt),
                ("v", "1.16.1"), ("c", self.client_name), ("f", "json")]
        form.extend(params)
        timeout = self.timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NavidromeError("Navidrome operation exceeded its time limit")
            # Bound both phases by the remaining budget. requests read timeout
            # is an inactivity timeout; every subsequent call rechecks deadline.
            timeout = tuple(min(limit, remaining / 2) for limit in self.timeout)
        try:
            response = self._session.post(
                f"{self.url}/rest/{endpoint}", data=form, timeout=timeout,
                verify=True, allow_redirects=False,
            )
        except requests.RequestException:
            raise NavidromeError(f"Navidrome {endpoint} request failed") from None
        # Redirects could send the authenticated form to another host. Reject
        # them even when requests would ordinarily treat them as successful.
        if not 200 <= response.status_code < 300:
            raise NavidromeError(f"Navidrome {endpoint} returned HTTP {response.status_code}")
        try:
            body = response.json()
        except (ValueError, requests.RequestException):
            raise NavidromeError(f"Navidrome {endpoint} returned invalid JSON") from None
        result = body.get("subsonic-response") if isinstance(body, dict) else None
        if not isinstance(result, dict) or result.get("status") != "ok":
            # Do not echo a server error message: it can contain arbitrary
            # request data, including authentication fields.
            raise NavidromeError(f"Navidrome {endpoint} did not report success")
        return result

    def connect(self, *, deadline: float | None = None) -> bool:
        self._call("ping", deadline=deadline)
        return True

    @staticmethod
    def _scan_state(response: dict[str, Any]) -> dict[str, Any]:
        state = response.get("scanStatus")
        if (not isinstance(state, dict) or type(state.get("scanning")) is not bool
                or type(state.get("count")) is not int or state["count"] < 0):
            raise NavidromeError("Navidrome scan status is unavailable or incomplete")
        return dict(state)

    def scan_status(self, *, deadline: float | None = None) -> dict[str, Any]:
        return self._scan_state(self._call("getScanStatus", deadline=deadline))

    def request_scan(self, *, deadline: float | None = None) -> dict[str, Any]:
        """Request a scan; the caller must use a separate admin service client."""
        return self._scan_state(self._call("startScan", deadline=deadline))

    @staticmethod
    def _scan_stamp(state: dict[str, Any]):
        if state["scanning"]:
            raise NavidromeError("Navidrome is scanning; try again after the scan completes")
        return tuple(state.get(key) for key in
                     ("count", "lastScan", "lastScanTime", "lastScanStarted", "lastScanCompleted"))

    def inventory(self, deadline: float | None = None) -> dict[str, dict[str, Any]]:
        """Read every song visible to these credentials, or return no inventory.

        Page until an empty result, even after a short page (server page caps
        vary). A global scan count is a change detector, not the expected count
        of this personal account's visible subset.
        """
        local_deadline = time.monotonic() + self.inventory_timeout
        deadline = min(deadline, local_deadline) if deadline is not None else local_deadline
        if time.monotonic() > deadline:
            raise NavidromeError("Navidrome inventory exceeded its time limit")
        before = self._scan_stamp(self.scan_status(deadline=deadline))
        songs: dict[str, dict[str, Any]] = {}
        offset = 0
        while True:
            if time.monotonic() > deadline:
                raise NavidromeError("Navidrome inventory exceeded its time limit")
            response = self._call("search3", [
                ("query", ""), ("artistCount", 0), ("albumCount", 0),
                ("songCount", self.page_size), ("songOffset", offset),
            ], deadline=deadline)
            result = response.get("searchResult3")
            if not isinstance(result, dict):
                raise NavidromeError("Navidrome returned an incomplete song inventory")
            page = result.get("song", [])
            if not isinstance(page, list):
                raise NavidromeError("Navidrome returned an invalid song inventory")
            for song in page:
                sid = song.get("id") if isinstance(song, dict) else None
                if not isinstance(sid, str) or not sid or sid in songs:
                    raise NavidromeError("Navidrome inventory has a missing or repeated song ID")
                songs[sid] = dict(song)
            if len(songs) > self.max_songs:
                raise NavidromeError("Navidrome inventory exceeded its song limit")
            if not page:
                if time.monotonic() > deadline:
                    raise NavidromeError("Navidrome inventory exceeded its time limit")
                if self._scan_stamp(self.scan_status(deadline=deadline)) != before:
                    raise NavidromeError("Navidrome library changed while reading the inventory")
                if time.monotonic() > deadline:
                    raise NavidromeError("Navidrome inventory exceeded its time limit")
                return songs
            offset += len(page)

    def _playlists(self, *, deadline: float | None = None) -> list[dict[str, Any]]:
        group = self._call("getPlaylists", deadline=deadline).get("playlists")
        if not isinstance(group, dict) or not isinstance(group.get("playlist", []), list):
            raise NavidromeError("Navidrome returned an incomplete playlist list")
        playlists = group.get("playlist", [])
        seen = set()
        for playlist in playlists:
            if (not isinstance(playlist, dict) or not isinstance(playlist.get("id"), str)
                    or not playlist["id"] or playlist["id"] in seen
                    or not isinstance(playlist.get("name"), str)):
                raise NavidromeError("Navidrome returned an invalid playlist list")
            seen.add(playlist["id"])
        return playlists

    def _playlist(self, playlist_id: str, *, deadline: float | None = None) -> dict[str, Any]:
        playlist = self._call("getPlaylist", [("id", playlist_id)], deadline=deadline).get("playlist")
        if not isinstance(playlist, dict) or playlist.get("id") != playlist_id:
            raise NavidromeError("Navidrome could not verify the requested playlist")
        return playlist

    def _owned(self, playlist: dict[str, Any], marker: str | None = None) -> None:
        if playlist.get("owner") != self.username:
            raise NavidromeError("Navidrome playlist belongs to another account or has no verified owner")
        if marker is not None and playlist.get("comment") != marker:
            raise NavidromeError("Refusing to modify an unmarked Navidrome playlist")
        if playlist.get("readonly") is True:
            raise NavidromeError("Navidrome playlist is read-only")

    @staticmethod
    def _exact_playlist(playlist: dict[str, Any], name: str, wanted: list[str]) -> bool:
        entries = playlist.get("entry", [])
        return (
            playlist.get("public") is False and playlist.get("name") == name
            and isinstance(entries, list)
            and all(isinstance(entry, dict) and isinstance(entry.get("id"), str) for entry in entries)
            and [entry["id"] for entry in entries] == wanted
            and playlist.get("songCount") == len(wanted)
        )

    def publish_playlist(
        self, name: str, song_ids: Iterable[str], state_playlist_id: str | None = None,
        marker: str = "",
        *, expected_paths: dict[str, str] | None = None, deadline: float | None = None,
    ) -> str:
        """Publish exactly these ordered IDs to a marked, personally owned playlist.

        Returns its ID only after verification. A failed multi-request write can
        leave a partial publisher-owned playlist; callers must not record it as
        successful. An unmarked existing playlist is never adopted or changed.
        Empty input is rejected, leaving prior publisher playlists untouched.
        When expected_paths is supplied, each fresh ID must still identify the
        exact absolute path selected during planning, even if the ID remains
        visible after a scan or a permissions change.
        """
        if not isinstance(name, str) or not name.strip() or not isinstance(marker, str) or not marker.strip():
            raise ValueError("A nonempty playlist name and stable ownership marker are required")
        if isinstance(song_ids, (str, bytes)):
            raise ValueError("song_ids must be an ordered iterable of IDs")
        wanted = list(song_ids)
        if not wanted:
            raise NavidromeError("Empty playlists are not published; existing playlists are left unchanged")
        if any(not isinstance(sid, str) or not sid for sid in wanted):
            raise ValueError("Every song ID must be a nonempty string")
        if state_playlist_id is not None and (not isinstance(state_playlist_id, str) or not state_playlist_id):
            raise ValueError("Stored playlist ID must be a nonempty string")

        if expected_paths is not None:
            if not isinstance(expected_paths, dict) or set(wanted) - expected_paths.keys():
                raise NavidromeError("Every requested song needs an expected absolute path")
            normalized_expected = {sid: absolute_song_path({"path": expected_paths[sid]}) for sid in set(wanted)}
        else:
            normalized_expected = None

        songs = self.inventory(deadline=deadline)
        for sid in set(wanted):
            if sid not in songs:
                raise NavidromeError("Requested song is not visible to this personal Navidrome account")
            live_path = absolute_song_path(songs[sid])
            if normalized_expected is not None and live_path != normalized_expected[sid]:
                raise NavidromeError("Navidrome song path changed since planning; playlist left unchanged")

        visible = self._playlists(deadline=deadline)
        same_name = [p for p in visible if p["name"].casefold() == name.casefold()]
        marked = [p for p in visible if p.get("comment") == marker]
        for playlist in same_name + marked:
            self._owned(playlist, marker)
        candidates = {p["id"] for p in same_name + marked}
        if len(candidates) > 1 or (state_playlist_id is not None and candidates - {state_playlist_id}):
            raise NavidromeError("Ambiguous Navidrome playlist ownership or name collision")
        playlist_id = state_playlist_id or next(iter(candidates), None)
        if playlist_id is None:
            # Do not add any music before the new playlist is confirmed private.
            created = self._call("createPlaylist", [("name", name)], deadline=deadline).get("playlist")
            if not isinstance(created, dict) or not isinstance(created.get("id"), str) or not created["id"]:
                raise NavidromeError("Navidrome created a playlist without a verifiable ID; no songs were added")
            playlist_id = created["id"]
            if playlist_id in {p["id"] for p in visible}:
                raise NavidromeError("Navidrome createPlaylist returned an existing playlist; no songs were added")
            self._owned(self._playlist(playlist_id, deadline=deadline))
        else:
            current = self._playlist(playlist_id, deadline=deadline)
            self._owned(current, marker)
            if self._exact_playlist(current, name, wanted):
                return playlist_id

        metadata = [("playlistId", playlist_id), ("name", name),
                    ("comment", marker), ("public", "false")]
        self._call("updatePlaylist", metadata, deadline=deadline)
        private = self._playlist(playlist_id, deadline=deadline)
        self._owned(private, marker)
        if private.get("public") is not False:
            raise NavidromeError("Navidrome did not confirm playlist privacy; no song list was written")

        self._call("createPlaylist", [("playlistId", playlist_id)] + [("songId", sid) for sid in wanted], deadline=deadline)
        # Some server versions reset metadata when replacing the song list.
        self._call("updatePlaylist", metadata, deadline=deadline)
        actual = self._playlist(playlist_id, deadline=deadline)
        self._owned(actual, marker)
        if not self._exact_playlist(actual, name, wanted):
            raise NavidromeError("Navidrome did not retain the private playlist's exact ordered song list")
        return playlist_id
