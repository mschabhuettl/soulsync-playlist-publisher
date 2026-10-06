#!/usr/bin/env python3
"""Personal Navidrome playlist publisher; standalone, no SoulSync code changes.

Default is a read-only planning run. --apply copies missing personal files and
updates only playlists carrying this publisher's per-source ownership marker.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.dont_write_bytecode = True

from soul_source import load_snapshot, load_service_settings, SourceError
from navidrome_api import NavClient, NavidromeError
from publisher_files import plan_track, install_copy, prepare_inventory, PublisherFileError


class PublisherError(ValueError):
    pass


def under(path, root):
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return False


def load_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    if config.get('version') != 1 or not isinstance(config.get('profiles'), dict):
        raise PublisherError('Configuration requires version 1 and profile rules')
    if not config['profiles']:
        raise PublisherError('No profiles configured')
    for name, rule in config['profiles'].items():
        if not name or not isinstance(rule, dict):
            raise PublisherError('Invalid profile rule')
        roots = rule.get('read_roots', [])
        write = rule.get('write_root')
        if not roots or not isinstance(roots, list) or not write:
            raise PublisherError('Each profile needs read_roots and write_root')
        for root in [*roots, write]:
            if not isinstance(root, str) or not Path(root).is_absolute() or Path(root).resolve() == Path('/'):
                raise PublisherError('Only absolute music folders are allowed')
            if not Path(root).is_dir():
                raise PublisherError(f'Configured music folder is unavailable: {root}')
        if not any(under(write, root) for root in roots):
            raise PublisherError('Personal download target must be readable')
        for other_name, other in config['profiles'].items():
            if other_name != name:
                if any(under(write, root) or under(root, write) for root in other.get('read_roots', [])):
                    raise PublisherError('Personal output overlaps another profile library')
    for root in config.get('source_roots', []):
        if not isinstance(root, str) or not Path(root).is_absolute() or Path(root).resolve() == Path('/'):
            raise PublisherError('Invalid source root')
    if not config.get('source_roots'):
        raise PublisherError('No permitted source folders')
    for key in ('soul_database', 'state_path'):
        if not Path(config.get(key, '')).is_absolute():
            raise PublisherError(f'{key} must be absolute')
    limit = config.get('max_copies_per_run', 10)
    runtime = config.get('max_runtime_seconds', 220)
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise PublisherError('max_copies_per_run must be between 1 and 100')
    if not isinstance(runtime, int) or not 10 <= runtime <= 240:
        raise PublisherError('max_runtime_seconds must be between 10 and 240')
    if not isinstance(config.get('playlist_prefix', 'SoulSync · '), str):
        raise PublisherError('playlist_prefix must be text')
    return config


def read_state(path):
    path = Path(path)
    if not path.exists():
        return {'version': 1, 'playlists': {}}
    if path.is_symlink():
        raise PublisherError('State file must not be a symlink')
    state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('version') != 1 or not isinstance(state.get('playlists'), dict):
        raise PublisherError('Invalid publisher state; refusing to replace it')
    return state


def save_state(path, state):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix='.publisher-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def run_lock(state_path, apply):
    if not apply:
        yield
        return
    directory = Path(state_path).parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = directory / 'publisher.lock'
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PublisherError('Another publisher run is already active') from exc
        yield
    finally:
        os.close(fd)


def _message(exc):
    # Do not expose arbitrary HTTP/SQL exception text, URLs or credential data.
    if isinstance(exc, (PublisherError, SourceError, NavidromeError, PublisherFileError)):
        return str(exc)
    return f'{type(exc).__name__}: operation could not be completed'


def run(config, *, apply=False, profile_names=None, playlist_ids=None,
        client_factory=NavClient, snapshot_loader=load_snapshot,
        settings_loader=load_service_settings, planner=plan_track, copier=install_copy):
    deadline = time.monotonic() + config.get('max_runtime_seconds', 220)
    profiles = list(profile_names or config['profiles'])
    if any(name not in config['profiles'] for name in profiles):
        raise PublisherError('Requested profile is not configured')
    settings = settings_loader(config['soul_database'], config.get('soul_config'))
    if settings.get('active_media_server') != 'navidrome':
        raise PublisherError('SoulSync active media server must be Navidrome')
    snapshot = snapshot_loader(config['soul_database'], profiles, playlist_ids)
    usernames = [p['navidrome_username'].casefold() for p in snapshot['profiles']]
    if len(usernames) != len(set(usernames)):
        raise PublisherError('Personal SoulSync profiles must use distinct Navidrome accounts')
    result = {'mode': 'apply' if apply else 'dry-run', 'copied': 0,
              'published': 0, 'unchanged': 0, 'pending': 0, 'errors': 0,
              'scan': 'not requested', 'playlists': []}
    with run_lock(config['state_path'], apply):
        state = read_state(config['state_path'])
        for profile in snapshot['profiles']:
            rule = config['profiles'][profile['name']]
            client = client_factory(settings['url'], profile['navidrome_username'],
                                    profile['navidrome_password'], client_name='SoulSync')
            try:
                inventory = client.inventory(deadline=deadline)
                inventory = prepare_inventory(inventory, rule, deadline=deadline)
            except Exception as exc:
                result['errors'] += 1
                result['playlists'].append({'profile': profile['name'], 'status': 'error',
                                            'reason': _message(exc)})
                continue
            for playlist in profile['playlists']:
                if time.monotonic() >= deadline:
                    result['pending'] += 1
                    result['playlists'].append({'profile': profile['name'], 'name': playlist['name'],
                                                'status': 'pending', 'reason': 'Run time limit; retry next run'})
                    continue
                entry = {'profile': profile['name'], 'id': playlist['id'],
                         'name': playlist['name'], 'status': 'pending', 'tracks': []}
                result['playlists'].append(entry)
                tracks = playlist['tracks']
                if not tracks:
                    entry['reason'] = 'Empty source playlist: existing destination is preserved'
                    result['pending'] += 1
                    continue
                song_ids = []
                expected_paths = {}
                pending = False
                for track in tracks:
                    if time.monotonic() >= deadline:
                        pending = True
                        entry['reason'] = 'Run time limit; retry next run'
                        break
                    try:
                        if track.get('blocked'):
                            pending = True
                            entry['tracks'].append({'title': track['title'], 'status': 'blocked',
                                                    'reason': 'Repair the manual metadata match in SoulSync first'})
                            continue
                        plan = planner(track, rule, config['source_roots'], inventory, deadline=deadline)
                        public = {key: plan.get(key) for key in ('status', 'source', 'target', 'song_id', 'reason') if plan.get(key) is not None}
                        public['title'] = track['title']
                        entry['tracks'].append(public)
                        if plan['status'] == 'ready':
                            song_ids.append(str(plan['song_id']))
                            expected_paths[str(plan['song_id'])] = plan['target'] or plan['source']
                        else:
                            pending = True
                            if plan['status'] == 'copy' and apply:
                                if result['copied'] < config.get('max_copies_per_run', 10):
                                    copier(plan, deadline=deadline)
                                    result['copied'] += 1
                                    public['status'] = 'copied; waiting for Navidrome scan'
                                else:
                                    public['reason'] = 'Copy limit; retry next run'
                    except Exception as exc:
                        pending = True
                        result['errors'] += 1
                        entry['tracks'].append({'title': track.get('title', ''), 'status': 'error',
                                                'reason': _message(exc)})
                if pending or len(song_ids) != len(tracks):
                    result['pending'] += 1
                    entry.setdefault('reason', 'All tracks must be available in the personal Navidrome account before publication')
                    continue
                key = f"{profile['id']}:{playlist['id']}"
                marker = f"soulsync-profile-publisher/v1 profile={profile['id']} playlist={playlist['id']}"
                target_name = config.get('playlist_prefix', 'SoulSync · ') + playlist['name']
                if not apply:
                    entry['status'] = 'ready to publish'
                    entry['target_name'] = target_name
                    continue
                try:
                    recorded = state['playlists'].get(key, {})
                    if recorded and recorded.get('username') != profile['navidrome_username']:
                        raise PublisherError('Saved account mapping changed; review state before publishing')
                    nav_id = client.publish_playlist(target_name, song_ids,
                                                     state_playlist_id=recorded.get('playlist_id'), marker=marker,
                                                     expected_paths=expected_paths, deadline=deadline)
                    state['playlists'][key] = {'playlist_id': nav_id, 'username': profile['navidrome_username'],
                                              'name': target_name, 'song_ids': song_ids}
                    save_state(config['state_path'], state)
                    result['published'] += 1
                    entry['status'] = 'published and verified'
                    entry['navidrome_playlist_id'] = nav_id
                except Exception as exc:
                    result['errors'] += 1
                    entry['status'] = 'error'
                    entry['reason'] = _message(exc)
        # A scan is requested only after all users have been processed. It can
        # finish between runs; no long sleeps or half-complete playlist writes.
        if apply and result['copied'] and config.get('request_scan_after_copy', True):
            try:
                service = client_factory(settings['url'], settings['username'], settings['password'], client_name='SoulSync')
                service.request_scan(deadline=deadline)
                result['scan'] = 'requested; run publisher again after scan completion'
            except Exception as exc:
                result['scan'] = 'not started; start a Navidrome scan, then retry'
                result['scan_error'] = _message(exc)
        if apply:
            save_state(Path(config['state_path']).with_name('last-report.json'), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/app/config/playlist-publisher.json')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--dry-run', action='store_true')
    parser.add_argument('--profile', action='append')
    parser.add_argument('--playlist-id', action='append', type=int)
    parser.add_argument('--list', action='store_true', help='List configured source playlists without contacting Navidrome')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.list:
            profiles = args.profile or list(config['profiles'])
            if any(name not in config['profiles'] for name in profiles):
                raise PublisherError('Requested profile is not configured')
            snapshot = load_snapshot(config['soul_database'], profiles, set(args.playlist_id) if args.playlist_id else None)
            public = [{'profile': p['name'], 'playlists': [
                {'id': pl['id'], 'name': pl['name'], 'tracks': len(pl['tracks'])} for pl in p['playlists']
            ]} for p in snapshot['profiles']]
            print(json.dumps(public, ensure_ascii=False, indent=2))
            return 0
        result = run(config, apply=args.apply, profile_names=args.profile,
                     playlist_ids=set(args.playlist_id) if args.playlist_id else None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result['errors'] else 0
    except Exception as exc:
        print(json.dumps({'status': 'error', 'reason': _message(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
