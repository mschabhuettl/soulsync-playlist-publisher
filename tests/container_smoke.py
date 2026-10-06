#!/usr/bin/env python3
"""Exercise the built image against a live WAL fixture and read-only mounts.

Run on a Linux Docker host after installing requirements.txt:
    python tests/container_smoke.py ghcr.io/owner/image:tag

The tested app runs as 3007:3007 with no network, a read-only root filesystem,
and a writable /tmp. A short root helper only sets/restores ownership inside
the disposable host fixture; it never mounts Docker's socket or real data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

from cryptography.fernet import Fernet


SCHEMA = """
CREATE TABLE profiles(id INTEGER PRIMARY KEY,name TEXT,navidrome_username TEXT,navidrome_password TEXT);
CREATE TABLE mirrored_playlists(id INTEGER PRIMARY KEY,profile_id INTEGER,name TEXT,source TEXT,custom_name TEXT);
CREATE TABLE mirrored_playlist_tracks(id INTEGER PRIMARY KEY,playlist_id INTEGER,position INTEGER,track_name TEXT,
  artist_name TEXT,album_name TEXT,duration_ms INTEGER,source_track_id TEXT,extra_data TEXT);
CREATE TABLE tracks(id TEXT PRIMARY KEY,title TEXT,artist_id TEXT,album_id TEXT,file_path TEXT,server_source TEXT,
  track_artist TEXT,duration INTEGER,spotify_track_id TEXT,deezer_id TEXT,isrc TEXT);
CREATE TABLE artists(id TEXT PRIMARY KEY,name TEXT);
CREATE TABLE albums(id TEXT PRIMARY KEY,title TEXT);
CREATE TABLE track_downloads(id INTEGER PRIMARY KEY,file_path TEXT,status TEXT,track_title TEXT,track_artist TEXT,
  track_album TEXT,spotify_track_id TEXT,deezer_track_id TEXT,isrc TEXT);
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
"""

PREPARE_PERMISSIONS = """
import os
from pathlib import Path
root = Path('/fixture')
for relative in ('source-data/.encryption_key', 'music/Alice', 'state'):
    os.chown(root / relative, 3007, 3007)
os.chmod(root / 'source-data/.encryption_key', 0o600)
os.chmod(root / 'music/Alice', 0o2770)
os.chmod(root / 'state', 0o770)
"""

RESTORE_PERMISSIONS = """
import os
import sys
from pathlib import Path
root = Path('/fixture')
uid, gid = map(int, sys.argv[1:])
for path in [root, *root.rglob('*')]:
    os.chown(path, uid, gid)
"""

RAW_CHECK = """
import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat

import requests
from cryptography.fernet import Fernet
from publisher_files import install_copy, plan_track
from soul_source import load_snapshot

assert os.getuid() == 3007 and os.getgid() == 3007
assert os.statvfs('/').f_flag & os.ST_RDONLY
assert os.statvfs('/app/data').f_flag & os.ST_RDONLY
assert os.statvfs('/app/config').f_flag & os.ST_RDONLY
assert not os.statvfs('/tmp').f_flag & os.ST_RDONLY
Path('/tmp/write-check').write_text('temporary files are allowed')
assert hasattr(ctypes.CDLL(None), 'renameat2')
assert stat.S_IMODE(Path('/app/data/.encryption_key').stat().st_mode) == 0o600
assert Path('/app/data/.encryption_key').stat().st_uid == 3007

snapshot = load_snapshot('/app/data/music_library.db', ['alice'])
profile = snapshot['profiles'][0]
assert profile['navidrome_password'] == 'smoke-test-only-password'
assert profile['playlists'][0]['id'] == 42
assert profile['playlists'][0]['name'] == 'Only committed in WAL'
assert len(profile['playlists'][0]['tracks']) == 1

os.umask(0o002)
source = Path('/music/Bob/song.flac')
before = hashlib.sha256(source.read_bytes()).hexdigest()
rule = {'read_roots': ['/music/Alice', '/music/Shared'], 'write_root': '/music/Alice'}
track = {'title': 'Song', 'artist': 'Artist', 'album': 'Album',
         'duration_ms': 200000, 'source_paths': [str(source)]}
plan = plan_track(track, rule, ['/music/Alice', '/music/Bob', '/music/Shared', '/app/Transfer'], {})
assert plan['status'] == 'copy', plan['status']
target = Path(install_copy(plan))
assert target.is_relative_to('/music/Alice/_SoulSync')
assert target.read_bytes() == source.read_bytes()
assert target.stat().st_ino != source.stat().st_ino
assert (target.stat().st_uid, target.stat().st_gid) == (3007, 3007)
assert (target.parent.stat().st_uid, target.parent.stat().st_gid) == (3007, 3007)
assert stat.S_IMODE(target.stat().st_mode) == 0o660
assert target.parent.stat().st_mode & stat.S_IWGRP
assert hashlib.sha256(source.read_bytes()).hexdigest() == before
assert Path(install_copy(plan)) == target
print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'copy_verified': True,
                  'wal_row_seen': True, 'credentials_decrypted': True}))
"""

SOURCE_FINGERPRINT = """
import hashlib
import json
from pathlib import Path
result = {}
for name, root in [('source-data', Path('/app/data')), ('source-config', Path('/app/config'))]:
    for path in sorted(root.rglob('*')):
        relative = name + '/' + path.relative_to(root).as_posix()
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'directory'
print(json.dumps(result, sort_keys=True))
"""


def command(args: list[str], *, timeout: int = 120) -> str:
    result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Docker check failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def mount(source: Path, destination: str, *, readonly: bool = True) -> list[str]:
    value = f"type=bind,src={source},dst={destination}"
    if readonly:
        value += ",readonly"
    return ['--mount', value]


def source_fingerprint(root: Path) -> dict[str, str]:
    result = {}
    for name in ('source-data', 'source-config'):
        for path in sorted((root / name).rglob('*')):
            relative = path.relative_to(root).as_posix()
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'directory'
    return result


def smoke(image: str) -> None:
    if shutil.which('docker') is None:
        raise RuntimeError('Docker is required for this integration smoke test')
    with tempfile.TemporaryDirectory(prefix='publisher-container-smoke-') as name:
        root = Path(name)
        for relative in ('source-data', 'source-config', 'settings', 'state', 'music',
                         'music/Alice', 'music/Bob', 'music/Shared', 'transfer'):
            path = root / relative
            path.mkdir(exist_ok=True)
            path.chmod(0o755)

        key = Fernet.generate_key()
        (root / 'source-data/.encryption_key').write_bytes(key)
        (root / 'source-config/config.json').write_text('{}', encoding='utf-8')
        (root / 'music/Bob/song.flac').write_bytes(b'container smoke audio-copy fixture\n')
        (root / 'source-config/config.json').chmod(0o644)
        (root / 'music/Bob/song.flac').chmod(0o644)
        config = {
            'version': 1,
            'soul_database': '/app/data/music_library.db',
            'soul_config': '/app/config/config.json',
            'state_path': '/state/state.json',
            'source_roots': ['/music/Alice', '/music/Bob', '/music/Shared', '/app/Transfer'],
            'profiles': {'alice': {'read_roots': ['/music/Alice', '/music/Shared'],
                                   'write_root': '/music/Alice'}},
        }
        (root / 'settings/publisher.json').write_text(json.dumps(config), encoding='utf-8')
        (root / 'settings/publisher.json').chmod(0o644)
        database = root / 'source-data/music_library.db'
        writer = sqlite3.connect(database)
        root_docker = ['docker', 'run', '--rm', '--network', 'none', '--read-only',
                       '--user', '0:0', '--entrypoint', 'python3',
                       *mount(root, '/fixture', readonly=False)]
        try:
            assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.executescript(SCHEMA)
            token = Fernet(key).encrypt(b'smoke-test-only-password').decode('ascii')
            writer.execute('INSERT INTO profiles VALUES(1,?,?,?)', ('alice', 'alice', token))
            writer.commit()
            writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            writer.execute("INSERT INTO mirrored_playlists VALUES(42,1,'Only committed in WAL','spotify',NULL)")
            writer.execute("INSERT INTO mirrored_playlist_tracks VALUES(1,42,0,'Song','Artist','Album',200000,'song1','{}')")
            writer.commit()

            # Prove the selected playlist is absent from the main DB bytes;
            # the application can only see it by reading the live WAL too.
            baseline_copy = root / 'baseline-copy.db'
            shutil.copyfile(database, baseline_copy)
            offline = sqlite3.connect(baseline_copy)
            try:
                assert offline.execute('SELECT count(*) FROM mirrored_playlists').fetchone()[0] == 0
            finally:
                offline.close()
            wal = database.with_name(database.name + '-wal')
            shm = database.with_name(database.name + '-shm')
            assert wal.is_file() and wal.stat().st_size > 0 and shm.is_file()
            for path in (database, wal, shm):
                path.chmod(0o644)
            before = source_fingerprint(root)
            command([*root_docker, image, '-B', '-c', PREPARE_PERMISSIONS])

            docker = ['docker', 'run', '--rm', '--network', 'none', '--read-only',
                      '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                      '--user', '3007:3007', '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m',
                      '--env', 'PYTHONDONTWRITEBYTECODE=1',
                      '--env', 'SOULSYNC_CONFIG_PATH=/app/config/config.json',
                      *mount(root / 'source-data', '/app/data'),
                      *mount(root / 'source-config', '/app/config'),
                      *mount(root / 'settings', '/config'),
                      *mount(root / 'state', '/state', readonly=False),
                      *mount(root / 'music', '/music', readonly=False),
                      *mount(root / 'transfer', '/app/Transfer')]

            output = command([*docker, '--entrypoint', 'python3', image, '-B', '-c', RAW_CHECK])
            assert json.loads(output)['copy_verified'] is True
            listed = json.loads(command([*docker, image, '--config', '/config/publisher.json', '--list']))
            assert listed == [{'profile': 'alice', 'playlists': [
                {'id': 42, 'name': 'Only committed in WAL', 'tracks': 1}]}], listed

            # First start with empty host directories, as Docker bind mounts
            # would create them. Only three capabilities are needed to prepare
            # publisher-owned directories and permanently drop root privileges.
            auto_config = root / 'auto-config'
            auto_config.mkdir(mode=0o755)
            rules = json.dumps(config['profiles'])
            automatic = ['docker', 'run', '--rm', '--network', 'none', '--read-only',
                         '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'SETUID',
                         '--cap-add', 'SETGID', '--security-opt', 'no-new-privileges:true',
                         '--user', '0:0', '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m',
                         '--env', 'PUID=3007', '--env', 'PGID=3007',
                         '--env', 'PROFILE_RULES=' + rules,
                         *mount(root / 'source-data', '/app/data'),
                         *mount(root / 'source-config', '/app/config'),
                         *mount(auto_config, '/config', readonly=False),
                         *mount(root / 'state', '/state', readonly=False),
                         *mount(root / 'music', '/music'),
                         *mount(root / 'music/Alice', '/music/Alice', readonly=False),
                         *mount(root / 'transfer', '/app/Transfer')]
            assert json.loads(command([*automatic, image, '--list'])) == listed
            inspect_config = "from pathlib import Path; import json; p=Path('/config/playlist-publisher.json'); s=p.stat(); print(json.dumps({'text':p.read_text(),'uid':s.st_uid,'gid':s.st_gid,'mode':s.st_mode & 0o777}))"
            inspection_command = [*automatic, '--user', '3007:3007', '--entrypoint', 'python3',
                                  image, '-B', '-c', inspect_config]
            generated_before = json.loads(command(inspection_command))
            assert json.loads(generated_before['text'])['profiles'] == config['profiles']
            assert (generated_before['uid'], generated_before['gid']) == (3007, 3007)
            assert generated_before['mode'] == 0o640
            assert json.loads(command([*automatic, '--env', 'PROFILE_RULES=invalid', image, '--list'])) == listed
            assert json.loads(command(inspection_command)) == generated_before
            after = json.loads(command([*docker, '--entrypoint', 'python3', image,
                                        '-B', '-c', SOURCE_FINGERPRINT]))
            assert after == before, 'Read-only source database, WAL, SHM or config contents changed'
            print('PASS: image dependencies, Linux atomic copy, UID/GID 3007, read-only mounts,')
            print('      live WAL visibility, Fernet credentials, actual --list entrypoint, and unchanged source files')
            print('      automatic first-start configuration, UID/GID drop, and preserved existing configuration')
        finally:
            writer.close()
            # GitHub runners are not root. Restore ownership so tempfile can
            # remove mode-0600 keys and group-writable generated directories.
            command([*root_docker, image, '-B', '-c', RESTORE_PERMISSIONS,
                     str(os.getuid()), str(os.getgid())])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', help='Locally built Docker image tag to test')
    args = parser.parse_args()
    smoke(args.image)


if __name__ == '__main__':
    main()
