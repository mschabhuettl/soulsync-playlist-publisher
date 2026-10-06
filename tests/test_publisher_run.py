import copy
import hashlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from publish_playlists import run, PublisherError, load_config


@pytest.fixture
def setup(tmp_path):
    roots = {name: tmp_path / name for name in ('Alice', 'Bob', 'Shared', 'Transfer')}
    for path in roots.values():
        path.mkdir()
    foreign = roots['Bob'] / 'song.flac'
    foreign.write_bytes(b'original-test-audio-bytes')
    shared = roots['Shared'] / 'shared.flac'
    shared.write_bytes(b'shared-test-audio-bytes')
    rules = {'alice': {'read_roots': [str(roots['Alice']), str(roots['Shared'])], 'write_root': str(roots['Alice'])},
             'bob': {'read_roots': [str(roots['Bob']), str(roots['Shared'])], 'write_root': str(roots['Bob'])}}
    config = {'version': 1, 'soul_database': str(tmp_path / 'unused.db'),
              'state_path': str(tmp_path / 'state' / 'state.json'), 'profiles': rules,
              'source_roots': [str(p) for p in roots.values()], 'max_copies_per_run': 10,
              'max_runtime_seconds': 220, 'request_scan_after_copy': True}
    track = {'key': 'source-1', 'title': 'Song', 'artist': 'Artist', 'album': 'Album',
             'duration_ms': 180000, 'source_paths': [str(foreign)]}
    snapshot = {'profiles': [
        {'id': 2, 'name': 'alice', 'navidrome_username': 'Alice', 'navidrome_password': 'alice-secret',
         'playlists': [{'id': 10, 'name': 'Favorites', 'tracks': [track]}]},
        {'id': 3, 'name': 'bob', 'navidrome_username': 'Bob', 'navidrome_password': 'bob-secret',
         'playlists': [{'id': 20, 'name': 'Favorites', 'tracks': [copy.deepcopy(track)]}]},
    ]}
    inventories = {'Alice': {}, 'Bob': {'n1': {'id': 'n1', 'title': 'Song', 'artist': 'Artist',
                                                 'album': 'Album', 'duration': 180, 'path': str(foreign)}}}
    calls = []

    class Client:
        def __init__(self, url, username, password, **kwargs):
            self.username = username

        def inventory(self, **kwargs):
            return inventories[self.username]

        def request_scan(self, **kwargs):
            calls.append(('scan', self.username))
            return {'scanning': True, 'count': 0}

        def publish_playlist(self, name, song_ids, **kwargs):
            assert all(sid in inventories[self.username] for sid in song_ids)
            for sid in song_ids:
                assert kwargs['expected_paths'][sid] == inventories[self.username][sid]['path']
            calls.append(('publish', self.username, list(song_ids), kwargs['marker']))
            return 'playlist-' + self.username

    def snapshot_loader(db, names, ids):
        result = copy.deepcopy(snapshot)
        result['profiles'] = [p for p in result['profiles'] if p['name'] in names]
        if ids is not None:
            for p in result['profiles']:
                p['playlists'] = [pl for pl in p['playlists'] if pl['id'] in ids]
        return result

    def execute(**kwargs):
        return run(config, client_factory=Client, snapshot_loader=snapshot_loader,
                   settings_loader=lambda *args: {'active_media_server': 'navidrome', 'url': 'http://navidrome',
                                                  'username': 'service-admin', 'password': 'admin-secret'}, **kwargs)
    return config, snapshot, inventories, calls, roots, execute


def files(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def test_dry_run_has_zero_local_writes_or_playlist_scan_calls(setup, tmp_path):
    config, snapshot, inv, calls, roots, execute = setup
    before = files(tmp_path)
    result = execute()
    assert files(tmp_path) == before
    assert calls == []
    assert result['copied'] == result['published'] == 0
    assert result['playlists'][0]['tracks'][0]['status'] == 'copy'
    assert not Path(config['state_path']).parent.exists()
    assert 'secret' not in str(result)


def test_foreign_only_track_becomes_independent_own_copy_then_personal_playlist(setup):
    config, snapshot, inv, calls, roots, execute = setup
    original = (roots['Bob'] / 'song.flac').read_bytes()
    first = execute(apply=True, profile_names=['alice'])
    assert first['copied'] == 1 and first['published'] == 0
    target = Path(first['playlists'][0]['tracks'][0]['target'])
    assert target.is_relative_to(roots['Alice'])
    assert target.read_bytes() == original
    assert target.stat().st_ino != (roots['Bob'] / 'song.flac').stat().st_ino
    assert calls == [('scan', 'service-admin')]
    inv['Alice'] = {'m1': {'id': 'm1', 'title': 'Song', 'artist': 'Artist', 'album': 'Album',
                             'duration': 180, 'path': str(target)}}
    second = execute(apply=True, profile_names=['alice'])
    assert second['copied'] == 0 and second['published'] == 1
    assert calls[-1] == ('publish', 'Alice', ['m1'], 'soulsync-profile-publisher/v1 profile=2 playlist=10')
    assert (roots['Bob'] / 'song.flac').read_bytes() == original


def test_both_reused_for_each_account_without_copy(setup):
    config, snapshot, inv, calls, roots, execute = setup
    for p in snapshot['profiles']:
        p['playlists'][0]['tracks'][0]['source_paths'] = [str(roots['Shared'] / 'shared.flac')]
    for username in inv:
        inv[username] = {'b1': {'id': 'b1', 'title': 'Song', 'artist': 'Artist', 'album': 'Album',
                                'duration': 180, 'path': str(roots['Shared'] / 'shared.flac')}}
    result = execute(apply=True)
    assert result['copied'] == 0 and result['published'] == 2
    assert {c[1] for c in calls if c[0] == 'publish'} == {'Alice', 'Bob'}
    assert all(c[0] != 'scan' for c in calls)
    assert not (roots['Alice'] / '_SoulSync').exists()


def test_missing_member_prevents_shortening_existing_playlist(setup):
    config, snapshot, inv, calls, roots, execute = setup
    snapshot['profiles'][1]['playlists'][0]['tracks'].append(
        {'key': 'missing', 'title': 'Unavailable', 'artist': 'Artist', 'album': 'Album', 'source_paths': []})
    result = execute(apply=True, profile_names=['bob'])
    assert result['published'] == 0 and result['pending'] == 1
    assert not any(c[0] == 'publish' for c in calls)


def test_empty_source_does_not_clear_destination(setup):
    _, snapshot, _, calls, _, execute = setup
    snapshot['profiles'][1]['playlists'][0]['tracks'] = []
    result = execute(apply=True, profile_names=['bob'])
    assert result['published'] == 0
    assert calls == []


def test_manually_unmatched_track_stays_blocked_even_if_metadata_matches(setup):
    _, snapshot, _, calls, _, execute = setup
    track = snapshot['profiles'][1]['playlists'][0]['tracks'][0]
    track.update(blocked=True, source_paths=[])
    result = execute(apply=True, profile_names=['bob'])
    assert result['published'] == 0 and result['pending'] == 1
    assert result['playlists'][0]['tracks'][0]['status'] == 'blocked'
    assert calls == []


def test_playlist_duplicates_keep_order(setup):
    _, snapshot, _, calls, _, execute = setup
    track = snapshot['profiles'][1]['playlists'][0]['tracks'][0]
    snapshot['profiles'][1]['playlists'][0]['tracks'].append(copy.deepcopy(track))
    result = execute(apply=True, profile_names=['bob'])
    assert result['published'] == 1
    assert calls[-1][2] == ['n1', 'n1']


def test_selected_profile_does_not_publish_other_person(setup):
    _, _, _, calls, _, execute = setup
    result = execute(apply=True, profile_names=['bob'], playlist_ids={20})
    assert result['published'] == 1
    assert calls == [('publish', 'Bob', ['n1'], 'soulsync-profile-publisher/v1 profile=3 playlist=20')]


def test_same_navidrome_login_on_both_profiles_is_rejected(setup):
    _, snapshot, _, calls, _, execute = setup
    snapshot['profiles'][0]['navidrome_username'] = 'Bob'
    with pytest.raises(PublisherError, match='distinct'):
        execute(apply=True)
    assert calls == []


def test_changed_saved_user_is_not_allowed_to_take_over_playlist(setup):
    config, snapshot, inv, calls, roots, execute = setup
    execute(apply=True, profile_names=['bob'])
    snapshot['profiles'][1]['navidrome_username'] = 'DifferentUser'
    inv['DifferentUser'] = inv['Bob']
    calls.clear()
    result = execute(apply=True, profile_names=['bob'])
    assert result['published'] == 0 and result['errors'] == 1
    assert calls == []


def test_config_rejects_personal_root_shared_with_other_user(setup, tmp_path):
    import json
    config, _, _, _, roots, _ = setup
    config['profiles']['alice']['write_root'] = str(roots['Shared'])
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    with pytest.raises(PublisherError, match='overlaps'):
        load_config(path)
