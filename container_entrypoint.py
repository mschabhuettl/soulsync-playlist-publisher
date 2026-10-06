#!/usr/bin/env python3
"""Create an initial configuration when requested, then run as the media user."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

sys.dont_write_bytecode = True

OWN_DIRECTORIES = (Path('/config'), Path('/state'))
SERVICE = Path(__file__).with_name('run_service.py')


class SetupError(ValueError):
    pass


def media_id(environ, name):
    try:
        value = int(environ.get(name, '3007'))
    except (TypeError, ValueError):
        raise SetupError(name + ' must be a non-root numeric ID') from None
    if not 1 <= value <= 2147483647:
        raise SetupError(name + ' must be a non-root numeric ID')
    return value


def drop_privileges(environ, *, prepare):
    if os.geteuid() != 0:
        return
    uid, gid = media_id(environ, 'PUID'), media_id(environ, 'PGID')
    if prepare:
        # Only publisher-owned mount roots. Never recurse or touch SoulSync/music.
        for directory in OWN_DIRECTORIES:
            if directory.is_symlink():
                raise SetupError('Publisher mount roots must not be symlinks')
            directory.mkdir(exist_ok=True, mode=0o750)
            info = directory.stat()
            if (info.st_uid, info.st_gid) != (uid, gid):
                os.chown(directory, uid, gid)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if os.geteuid() == 0:
        raise SetupError('Refusing to run the publisher as root')


def initial_config(environ):
    from publish_playlists import PublisherError
    try:
        profiles = json.loads(environ.get('PROFILE_RULES', ''))
    except (ValueError, TypeError):
        raise SetupError('Set PROFILE_RULES to a JSON object for first-start setup') from None
    if not isinstance(profiles, dict) or not profiles:
        raise SetupError('PROFILE_RULES must contain at least one profile')
    roots = []
    for rule in profiles.values():
        if not isinstance(rule, dict) or not isinstance(rule.get('read_roots'), list):
            raise PublisherError('Each profile needs read_roots and write_root')
        for root in rule['read_roots']:
            if root not in roots:
                roots.append(root)
    if '/app/Transfer' not in roots:
        roots.append('/app/Transfer')
    if 'SOURCE_ROOTS' in environ:
        try:
            roots = json.loads(environ['SOURCE_ROOTS'])
        except (ValueError, TypeError):
            raise SetupError('SOURCE_ROOTS must be a JSON array') from None
        if not isinstance(roots, list):
            raise SetupError('SOURCE_ROOTS must be a JSON array')
    return {
        'version': 1,
        'soul_database': '/app/data/music_library.db',
        'soul_config': environ.get('SOULSYNC_CONFIG_PATH', '/app/config/config.json'),
        'state_path': '/state/state.json',
        'playlist_prefix': 'SoulSync · ',
        'max_copies_per_run': 10,
        'max_runtime_seconds': 220,
        'request_scan_after_copy': True,
        'source_roots': roots,
        'profiles': profiles,
    }


def ensure_config(path, environ):
    from publish_playlists import load_config
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise SetupError('CONFIG_PATH must be an absolute non-symlink file path')
    if path.exists():
        if not path.is_file():
            raise SetupError('CONFIG_PATH exists but is not a file')
        return False  # Existing files and their ownership are never overwritten.
    config = initial_config(environ)
    fd, temporary = tempfile.mkstemp(prefix='.publisher-config-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o640)
        load_config(temporary)  # Reject invalid or overlapping roots before installation.
        try:
            os.link(temporary, path)  # Atomic create-if-absent, never replace a config.
        except FileExistsError:
            return False
        return True
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    os.umask(0o002)
    try:
        # Explicit --config paths belong to the one-shot caller, not bootstrap.
        skip_setup = args == ['healthcheck'] or '--help' in args or '-h' in args
        explicit_config = any(a == '--config' or a.startswith('--config=') for a in args)
        drop_privileges(os.environ, prepare=not skip_setup)
        if not skip_setup and not explicit_config:
            created = ensure_config(os.environ.get('CONFIG_PATH', '/config/playlist-publisher.json'), os.environ)
            if created:
                print('[publisher-setup] Created initial configuration from PROFILE_RULES', file=sys.stderr)
        os.execv(sys.executable, [sys.executable, '-B', str(SERVICE), *args])
    except Exception as exc:
        from publish_playlists import PublisherError
        message = str(exc) if isinstance(exc, (SetupError, PublisherError)) else type(exc).__name__
        print('[publisher-setup] ' + message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
