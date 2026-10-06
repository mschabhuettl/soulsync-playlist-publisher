"""Read an existing SoulSync index without importing or mutating SoulSync.

The returned passwords are secrets: keep the snapshot in memory and never log
or serialize it. Candidate paths are evidence, not authorization to access or
publish a file; the publisher must validate real paths, existence and output
roots. This adapter deliberately does not reproduce SoulSync's fuzzy matcher.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import unicodedata
from collections import defaultdict


class SourceError(RuntimeError):
    """A safe-to-display source error; never contains a credential or DB value."""


_REQUIRED = {
    'profiles': {'id', 'name', 'navidrome_username', 'navidrome_password'},
    'mirrored_playlists': {'id', 'profile_id', 'name', 'source'},
    'mirrored_playlist_tracks': {'id', 'playlist_id', 'position', 'track_name', 'artist_name',
                                'album_name', 'duration_ms', 'source_track_id', 'extra_data'},
    'tracks': {'id', 'title', 'artist_id', 'album_id', 'file_path', 'server_source'},
    'artists': {'id', 'name'},
    'albums': {'id', 'title'},
    'track_downloads': {'id', 'file_path', 'status', 'track_title', 'track_artist', 'track_album'},
}
_EXTERNAL = {
    'spotify': ('spotify_track_id', 'spotify_track_id'),
    'deezer': ('deezer_id', 'deezer_track_id'),
    'itunes': ('itunes_track_id', 'itunes_track_id'),
    'tidal': ('tidal_id', 'tidal_track_id'),
    'qobuz': ('qobuz_id', 'qobuz_track_id'),
    'musicbrainz': ('musicbrainz_recording_id', 'musicbrainz_recording_id'),
    'audiodb': ('audiodb_id', 'audiodb_id'),
    'isrc': ('isrc', 'isrc'),
}


def _text(value):
    return str(value).strip() if value is not None else ''


def _norm(value):
    # Keep punctuation and qualifiers: live/remastered/acoustic variants must
    # not collapse into the same recording merely because their titles resemble.
    return ' '.join(unicodedata.normalize('NFKC', _text(value)).casefold().split())


def _integer(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _extra(value):
    if value in (None, ''):
        return {}
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        raise SourceError('A mirrored track contains invalid metadata; snapshot aborted.') from None
    if not isinstance(result, dict):
        raise SourceError('A mirrored track has unsupported metadata; snapshot aborted.')
    return result


def _provider(value):
    provider = _text(value).casefold()
    return {'spotify_public': 'spotify', 'apple': 'itunes', 'apple_music': 'itunes',
            'musicbrainz_recording': 'musicbrainz'}.get(provider, provider)


def _external_id(provider, value):
    value = _text(value)
    prefix = f'{provider}:track:'
    return value[len(prefix):] if value.startswith(prefix) else value


def _artist_name(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _text(value.get('name'))
    if isinstance(value, list):
        return ', '.join(filter(None, (_artist_name(item) for item in value)))
    return ''


def _key_file(db_path, key_path, config_path=None):
    if key_path is not None:
        choices = [Path(key_path)]
    else:
        choices = [db_path.parent / '.encryption_key']
        config_path = Path(config_path or os.environ.get('SOULSYNC_CONFIG_PATH', '/app/config/config.json'))
        choices.append(config_path.parent / '.encryption_key')
    for candidate in choices:
        if candidate.is_file():
            return candidate
    raise SourceError('The existing SoulSync encryption key was not found.')


def _password(token, db_path, key_path, config_path=None):
    token = token if isinstance(token, str) else ''
    if not token:
        raise SourceError('A selected profile has no personal Navidrome login.')
    if not token.startswith('gAAAAA'):
        return token  # SoulSync's supported pre-encryption migration format.
    try:
        # Already a SoulSync runtime dependency. Importing its SettingsManager
        # would initialize/migrate the app DB and might generate a new key.
        from cryptography.fernet import Fernet
        key = _key_file(db_path, key_path, config_path).read_bytes()
        value = Fernet(key).decrypt(token.encode('ascii')).decode('utf-8')
    except SourceError:
        raise
    except Exception:
        raise SourceError('A personal Navidrome password could not be decrypted.') from None
    if not value:
        raise SourceError('A selected profile has an empty Navidrome password.')
    return value


def _schema(conn):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {}
    for table, required in _REQUIRED.items():
        if table not in tables:
            raise SourceError(f'Unsupported SoulSync schema: missing {table} table.')
        columns[table] = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if not required <= columns[table]:
            raise SourceError(f'Unsupported SoulSync schema: incomplete {table} table.')
    if 'manual_library_track_matches' in tables:
        columns['manual_library_track_matches'] = {
            row[1] for row in conn.execute('PRAGMA table_info(manual_library_track_matches)')}
    return columns


class _Candidates:
    def __init__(self, conn, columns):
        self.by_name = defaultdict(list)
        self.by_external = defaultdict(list)
        self.by_id = {}
        self.manual = defaultdict(list)
        sql = ("SELECT t.*, ar.name AS source_artist, al.title AS source_album "
               "FROM tracks t LEFT JOIN artists ar ON ar.id=t.artist_id "
               "LEFT JOIN albums al ON al.id=t.album_id "
               "WHERE t.server_source IN ('navidrome', 'soulsync') ORDER BY t.id")
        for record in conn.execute(sql):
            row = dict(record)
            candidate = self._add(row, row.get('title'), row.get('track_artist') or row.get('source_artist'),
                                  row.get('source_album'), row.get('duration'), download=False)
            if candidate:
                self.by_id[str(row['id'])] = candidate
        for record in conn.execute("SELECT * FROM track_downloads WHERE status='completed' ORDER BY id DESC"):
            row = dict(record)
            self._add(row, row['track_title'], row['track_artist'], row['track_album'], None, download=True)
        cols = columns.get('manual_library_track_matches', set())
        required = {'id', 'profile_id', 'source', 'source_track_id', 'library_track_id'}
        if cols and not required <= cols:
            raise SourceError('Unsupported SoulSync schema: incomplete manual match table.')
        if cols:
            for record in conn.execute('SELECT * FROM manual_library_track_matches ORDER BY id'):
                row = dict(record)
                candidate = self.by_id.get(str(row['library_track_id']))
                if candidate:
                    key = (int(row['profile_id']), _provider(row['source']), _text(row['source_track_id']))
                    self.manual[key].append(candidate)

    def _add(self, row, title, artist, album, duration, *, download):
        path = _text(row.get('file_path'))
        if not path or '\x00' in path or not path.startswith('/'):
            return None
        candidate = {'path': path, 'title': _norm(title), 'artist': _norm(artist),
                     'album': _norm(album), 'duration': _integer(duration)}
        if not candidate['title'] or not candidate['artist']:
            return None
        self.by_name[(candidate['title'], candidate['artist'])].append(candidate)
        for provider, fields in _EXTERNAL.items():
            value = _external_id(provider, row.get(fields[1 if download else 0]))
            if value:
                self.by_external[(provider, value)].append(candidate)
        return candidate

    @staticmethod
    def _consistent(candidate, identity):
        if (candidate['title'], candidate['artist']) != (_norm(identity['title']), _norm(identity['artist'])):
            return False
        left, right = candidate['duration'], identity['duration_ms']
        return not (left and right and abs(left - right) > max(2000, round(right * 0.02)))

    def paths(self, identity, external_ids, *, profile_id, original_source_id, provider, cached_id):
        candidates = []
        for pair in external_ids:
            candidates.extend(self.by_external.get(pair, []))
        candidates.extend(self.manual.get((profile_id, provider, original_source_id), []))
        if cached_id is not None:
            cached = self.by_id.get(str(cached_id))
            album = _norm(identity['album'])
            if cached and album and cached['album'] == album:
                candidates.append(cached)
        candidates = [c for c in candidates if self._consistent(c, identity)]
        if not candidates:
            candidates = [c for c in self.by_name.get((_norm(identity['title']), _norm(identity['artist'])), [])
                          if self._consistent(c, identity)]
            album = _norm(identity['album'])
            if album:
                candidates = [c for c in candidates if c['album'] == album]
            elif len({c['album'] for c in candidates}) > 1:
                # No provider ID / edition information to choose between albums.
                candidates = []
        # Multiple exact copies are intentional; the publisher chooses an
        # accessible personal/shared copy or copies one into the target root.
        return list(dict.fromkeys(c['path'] for c in candidates))


def _track(row, source, profile_id, candidates):
    extra = _extra(row['extra_data'])
    # Keep source membership intact: an unresolved discovery row must block
    # this playlist, not silently shorten it. A later successful manual fix
    # takes precedence over a stale unmatched_by_user flag upstream.
    manual_fixed = bool(extra.get('discovered') and extra.get('manual_match'))
    blocked = bool(extra.get('unmatched_by_user') and not manual_fixed)
    matched = extra.get('matched_data') if extra.get('discovered') else None
    if matched is not None and not isinstance(matched, dict):
        raise SourceError('A mirrored track has an unsupported discovery match.')
    matched = matched or {}
    title = _text(matched.get('name')) or _text(row['track_name'])
    artist = _artist_name(matched.get('artists')) or _text(row['artist_name'])
    album_value = matched.get('album')
    album = _text(album_value.get('name')) if isinstance(album_value, dict) else _text(album_value)
    album = album or _text(row['album_name'])
    duration = _integer(matched.get('duration_ms')) or _integer(row['duration_ms'])
    identity = {'title': title, 'artist': artist, 'album': album, 'duration_ms': duration}
    source_id = _external_id(source, row['source_track_id'])
    provider = _provider(extra.get('provider') or source)
    external_ids = []
    if source_id and source in _EXTERNAL:
        external_ids.append((source, source_id))
    matched_id = _external_id(provider, matched.get('id'))
    if matched_id and provider in _EXTERNAL and not extra.get('wing_it_fallback'):
        external_ids.append((provider, matched_id))
    external = matched.get('external_ids')
    if isinstance(external, dict) and external.get('isrc'):
        external_ids.append(('isrc', _text(external['isrc'])))
    if source_id:
        key = f'{source}:{source_id}'
    else:
        content = json.dumps([_norm(title), _norm(artist), _norm(album), duration], ensure_ascii=False)
        key = 'metadata:' + hashlib.sha256(content.encode('utf-8')).hexdigest()
    identity['key'] = key
    if blocked:
        identity.update(blocked=True, reason='manual_metadata_repair_required', source_paths=[])
    else:
        identity['source_paths'] = candidates.paths(identity, external_ids, profile_id=profile_id,
                                                    original_source_id=source_id, provider=source,
                                                    cached_id=extra.get('library_track_id'))
    return identity


def load_snapshot(db_path=None, wanted_profiles=None, playlist_ids=None, *, key_path=None):
    """Read one consistent snapshot of selected personal mirrored playlists.

    No database writes, migrations, cache touch timestamps, network requests,
    or file searches. Only completed download provenance is eligible. Path
    existence/allowed-root checks belong to the publisher. Missing profiles,
    credentials, selected playlists, or required schema abort explicitly.
    """
    db_path = Path(db_path or os.environ.get('DATABASE_PATH', '/app/data/music_library.db')).resolve()
    wanted_profiles = list(dict.fromkeys(wanted_profiles or []))
    if not wanted_profiles or any(not isinstance(name, str) or not name.strip() for name in wanted_profiles):
        raise SourceError('At least one exact SoulSync profile name is required.')
    selected_ids = None if playlist_ids is None else {int(value) for value in playlist_ids}
    if selected_ids is not None and not selected_ids:
        raise SourceError('An explicit playlist selection must not be empty.')
    try:
        conn = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=15)
    except sqlite3.Error:
        raise SourceError('The existing SoulSync database could not be opened read-only.') from None
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        columns = _schema(conn)
        placeholders = ','.join('?' for _ in wanted_profiles)
        rows = [dict(row) for row in conn.execute(f'SELECT * FROM profiles WHERE name IN ({placeholders}) ORDER BY id', wanted_profiles)]
        if len(rows) != len(wanted_profiles) or {row['name'] for row in rows} != set(wanted_profiles):
            raise SourceError('A selected SoulSync profile is missing or ambiguous.')
        candidates = _Candidates(conn, columns)
        profiles = []
        seen_playlists = set()
        for row in rows:
            username = _text(row['navidrome_username'])
            if not username:
                raise SourceError('A selected profile has no personal Navidrome login.')
            profile = {'id': int(row['id']), 'name': row['name'], 'navidrome_username': username,
                       'navidrome_password': _password(row['navidrome_password'], db_path, key_path), 'playlists': []}
            for playlist in conn.execute('SELECT * FROM mirrored_playlists WHERE profile_id=? ORDER BY id', (row['id'],)):
                playlist = dict(playlist)
                pid = int(playlist['id'])
                if selected_ids is not None and pid not in selected_ids:
                    continue
                tracks = []
                for track in conn.execute('SELECT * FROM mirrored_playlist_tracks WHERE playlist_id=? ORDER BY position, id', (pid,)):
                    parsed = _track(dict(track), _provider(playlist['source']), int(row['id']), candidates)
                    if parsed is not None:
                        tracks.append(parsed)
                name = _text(playlist.get('custom_name')) or _text(playlist['name'])
                if not name:
                    raise SourceError('A selected mirrored playlist has no name.')
                profile['playlists'].append({'id': pid, 'name': name, 'tracks': tracks})
                seen_playlists.add(pid)
            profiles.append(profile)
        if selected_ids is not None and seen_playlists != selected_ids:
            raise SourceError('A selected playlist is missing or belongs to a different profile.')
        return {'profiles': profiles}
    except SourceError:
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        raise SourceError('The SoulSync snapshot could not be read; no playlists were published.') from None
    finally:
        conn.close()


def load_service_settings(db_path=None, config_path=None, key_path=None):
    """Read service settings with SoulSync's DB-before-JSON priority.

    Unlike the app, corruption is an explicit error; this read-only adapter
    never initializes defaults, migrates settings, or repairs encryption keys.
    The caller must validate URL equality before transmitting credentials.
    """
    db_path = Path(db_path or os.environ.get('DATABASE_PATH', '/app/data/music_library.db')).resolve()
    config_path = Path(os.environ.get('SOULSYNC_CONFIG_PATH') or config_path or '/app/config/config.json')
    conn = None
    try:
        conn = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=15)
        conn.execute('PRAGMA query_only=ON')
        row = conn.execute("SELECT value FROM metadata WHERE key='app_config'").fetchone()
        config = json.loads(row[0]) if row and row[0] else None
        if config is not None and not isinstance(config, dict):
            raise SourceError('The SoulSync service settings have an unsupported format.')
        if not config:
            if not config_path.is_file():
                raise SourceError('No existing SoulSync service settings were found.')
            config = json.loads(config_path.read_text(encoding='utf-8'))
        if not isinstance(config, dict):
            raise SourceError('The SoulSync service settings have an unsupported format.')
        nav = config.get('navidrome') or {}
        soulseek = config.get('soulseek') or {}
        if not isinstance(nav, dict) or not isinstance(soulseek, dict):
            raise SourceError('The SoulSync service settings have an unsupported format.')
        password = nav.get('password')
        return {
            'url': _text(nav.get('base_url')),
            'username': _text(nav.get('username')),
            'password': _password(password, db_path, key_path, config_path) if password else '',
            'transfer_path': _text(soulseek.get('transfer_path', './Transfer')),
            'active_media_server': _text(config.get('active_media_server', 'plex')),
        }
    except SourceError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise SourceError('The SoulSync service settings could not be read safely.') from None
    finally:
        if conn is not None:
            conn.close()
