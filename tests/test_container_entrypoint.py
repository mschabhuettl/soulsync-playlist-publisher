import json
from pathlib import Path
import stat
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import container_entrypoint as entry
from publish_playlists import PublisherError


@pytest.fixture
def setup(tmp_path):
    for name in ('Alice', 'Bob', 'Shared', 'config'):
        (tmp_path / name).mkdir()
    rules = {
        'alice': {'read_roots': [str(tmp_path / 'Alice'), str(tmp_path / 'Shared')],
                  'write_root': str(tmp_path / 'Alice')},
        'bob': {'read_roots': [str(tmp_path / 'Bob'), str(tmp_path / 'Shared')],
                'write_root': str(tmp_path / 'Bob')},
    }
    return tmp_path / 'config/config.json', {'PROFILE_RULES': json.dumps(rules)}


def test_first_start_creates_valid_config_as_current_user(setup):
    path, env = setup
    assert entry.ensure_config(path, env)
    config = json.loads(path.read_text())
    assert config['profiles'] == json.loads(env['PROFILE_RULES'])
    assert config['source_roots'][-1] == '/app/Transfer'
    assert len(config['source_roots']) == len(set(config['source_roots']))
    assert config['state_path'] == '/state/state.json'
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.stat().st_uid == entry.os.geteuid()
    assert list(path.parent.iterdir()) == [path]


def test_existing_config_is_never_overwritten_or_reparsed(setup):
    path, _ = setup
    path.write_text('user-managed contents')
    before = path.stat()
    assert entry.ensure_config(path, {'PROFILE_RULES': 'invalid'}) is False
    assert path.read_text() == 'user-managed contents'
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_invalid_rules_leave_no_partial_file(setup):
    path, env = setup
    rules = json.loads(env['PROFILE_RULES'])
    rules['alice']['write_root'] = rules['bob']['write_root']
    with pytest.raises(PublisherError):
        entry.ensure_config(path, {'PROFILE_RULES': json.dumps(rules)})
    assert list(path.parent.iterdir()) == []


def test_create_race_preserves_winner(setup, monkeypatch):
    path, env = setup
    def race(source, target):
        Path(target).write_text('concurrent configuration')
        raise FileExistsError()
    monkeypatch.setattr(entry.os, 'link', race)
    assert entry.ensure_config(path, env) is False
    assert path.read_text() == 'concurrent configuration'
    assert list(path.parent.iterdir()) == [path]


def test_symlink_config_is_rejected(setup, tmp_path):
    path, env = setup
    source = tmp_path / 'untouched'
    source.write_text('original')
    path.symlink_to(source)
    with pytest.raises(entry.SetupError):
        entry.ensure_config(path, env)
    assert source.read_text() == 'original'


@pytest.mark.parametrize('value', ['0', '-1', 'root', '', '2147483648'])
def test_root_or_invalid_media_id_is_rejected(value):
    with pytest.raises(entry.SetupError):
        entry.media_id({'PUID': value}, 'PUID')


def test_root_changes_only_own_mount_roots_then_drops_groups_and_ids(tmp_path, monkeypatch):
    directories = (tmp_path / 'config', tmp_path / 'state')
    for p in directories:
        p.mkdir()
        (p / 'existing').write_text('preserved')
    calls = []
    monkeypatch.setattr(entry, 'OWN_DIRECTORIES', directories)
    monkeypatch.setattr(entry.os, 'geteuid', lambda: 0 if not calls or calls[-1][0] != 'uid' else 3007)
    monkeypatch.setattr(entry.os, 'chown', lambda p,u,g: calls.append(('chown', p,u,g)))
    monkeypatch.setattr(entry.os, 'setgroups', lambda groups: calls.append(('groups', groups)))
    monkeypatch.setattr(entry.os, 'setgid', lambda gid: calls.append(('gid', gid)))
    monkeypatch.setattr(entry.os, 'setuid', lambda uid: calls.append(('uid', uid)))
    entry.drop_privileges({}, prepare=True)
    assert calls == [('chown', p,3007,3007) for p in directories] + [('groups', []), ('gid',3007), ('uid',3007)]
    assert all((p / 'existing').read_text() == 'preserved' for p in directories)


def test_nonroot_execution_needs_no_root_capabilities(monkeypatch):
    monkeypatch.setattr(entry.os, 'geteuid', lambda: 3007)
    monkeypatch.setattr(entry.os, 'chown', lambda *args: pytest.fail('must not chown'))
    entry.drop_privileges({}, prepare=True)


def test_missing_profile_rules_gives_actionable_error(setup):
    path, _ = setup
    with pytest.raises(entry.SetupError, match='PROFILE_RULES'):
        entry.ensure_config(path, {})
    assert not path.exists()
