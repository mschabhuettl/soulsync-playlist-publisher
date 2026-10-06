import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_service as service


class Clock:
    def __init__(self):
        self.seconds = 0

    def time(self):
        return 1000 + self.seconds

    def monotonic(self):
        return self.seconds


class Event:
    def __init__(self, clock, stop_at):
        self.clock = clock
        self.stop_at = stop_at
        self.stopped = False

    def is_set(self):
        return self.stopped or self.clock.seconds >= self.stop_at

    def wait(self, timeout):
        self.clock.seconds += timeout
        return self.is_set()


class Child:
    def __init__(self, clock, duration, code=0, ignore_terminate=False):
        self.clock = clock
        self.end = clock.seconds + duration
        self.code = code
        self.terminated = False
        self.killed = False
        self.reaped = False
        self.ignore_terminate = ignore_terminate

    def poll(self):
        if self.killed:
            return -9
        if self.terminated and not self.ignore_terminate:
            return -15
        return self.code if self.clock.seconds >= self.end else None

    def wait(self, timeout=None):
        if self.poll() is None:
            if timeout is not None:
                self.clock.seconds += timeout
            if self.poll() is None:
                raise subprocess.TimeoutExpired('publisher', timeout)
        self.reaped = True
        return self.poll()

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_defaults_are_dry_run_and_temporary_heartbeat():
    settings = service.Settings.from_environment({})
    assert settings.mode == 'dry-run'
    assert settings.interval_seconds == 300
    assert settings.config_path == '/config/playlist-publisher.json'
    assert settings.heartbeat_path == '/tmp/heartbeat.json'
    assert service.publisher_command(settings)[-1] == '--dry-run'


@pytest.mark.parametrize('mode', ['yes', 'true', 'APPLY', '', 'apply --list'])
def test_invalid_mode_never_starts_apply(mode):
    with pytest.raises(service.ServiceError, match='MODE'):
        service.Settings.from_environment({'MODE': mode})


@pytest.mark.parametrize('interval', ['0', '-1', '29', '86401', 'nan', '1.5', ''])
def test_invalid_interval_rejected(interval):
    with pytest.raises(service.ServiceError, match='INTERVAL_SECONDS'):
        service.Settings.from_environment({'INTERVAL_SECONDS': interval})


@pytest.mark.parametrize('key', ['CONFIG_PATH', 'HEARTBEAT_PATH'])
def test_relative_paths_rejected(key):
    with pytest.raises(service.ServiceError, match='absolute'):
        service.Settings.from_environment({key: 'relative/path'})


def test_explicit_mode_config_and_one_shot():
    settings = service.Settings.from_environment({
        'MODE': 'apply', 'CONFIG_PATH': '/other/config.json',
        'INTERVAL_SECONDS': '60', 'HEARTBEAT_PATH': '/tmp/health.json'})
    assert service.publisher_command(settings)[-3:] == ['--config', '/other/config.json', '--apply']
    once = service.publisher_command(settings, ['--list', '--profile', 'alice'])
    assert once[-5:] == ['--config', '/other/config.json', '--list', '--profile', 'alice']
    assert '--apply' not in once
    for flags in [['--config=/custom.json', '--list'], ['--config', '/custom.json', '--list']]:
        explicit = service.publisher_command(settings, flags)
        assert '/other/config.json' not in explicit
        assert explicit[-len(flags):] == flags


def test_one_shot_uses_exec_and_does_not_create_service_state(monkeypatch):
    monkeypatch.setenv('CONFIG_PATH', '/custom/config.json')
    seen = []
    monkeypatch.setattr(service.os, 'execv', lambda executable, args: seen.append((executable, args)))
    monkeypatch.setattr(service, 'Supervisor', lambda *args: pytest.fail('Scheduler must not run'))
    service.main(['--dry-run', '--profile', 'alice'])
    assert len(seen) == 1
    assert seen[0][1][-5:] == ['--config', '/custom/config.json', '--dry-run', '--profile', 'alice']


def test_heartbeat_written_atomically_and_stale_or_failed_is_unhealthy(tmp_path):
    heartbeat = tmp_path / 'heartbeat.json'
    status = {'timestamp': 1000, 'pid': 123, 'phase': 'idle', 'last_exit_code': 0}
    service.write_heartbeat(heartbeat, status)
    assert json.loads(heartbeat.read_text()) == status
    assert list(tmp_path.iterdir()) == [heartbeat]
    assert service.heartbeat_healthy(heartbeat, now=1045)
    assert not service.heartbeat_healthy(heartbeat, now=1046)
    assert not service.heartbeat_healthy(heartbeat, now=999)
    status['last_exit_code'] = 1
    status['phase'] = 'running'
    service.write_heartbeat(heartbeat, status)
    assert not service.heartbeat_healthy(heartbeat, now=1000)
    status['last_exit_code'] = 0
    status['phase'] = 'stopped'
    service.write_heartbeat(heartbeat, status)
    assert not service.heartbeat_healthy(heartbeat, now=1000)


@pytest.mark.parametrize('contents', ['{}', '[]', 'null', 'not json',
    '{"timestamp": true}', '{"timestamp": "1000"}'])
def test_invalid_heartbeat_is_unhealthy(tmp_path, contents):
    heartbeat = tmp_path / 'heartbeat.json'
    heartbeat.write_text(contents)
    assert not service.heartbeat_healthy(heartbeat, now=1000)


def test_heartbeat_symlinks_are_rejected(tmp_path):
    target = tmp_path / 'target'
    target.write_text('preserved')
    link = tmp_path / 'link'
    link.symlink_to(target)
    with pytest.raises(service.ServiceError, match='symlink'):
        service.write_heartbeat(link, {})
    assert not service.heartbeat_healthy(link)
    assert target.read_text() == 'preserved'


def test_sequential_runs_and_health_recover_after_success():
    clock = Clock()
    event = Event(clock, stop_at=40)
    children = []
    snapshots = []
    starts = []

    def spawn(command):
        assert all(child.reaped for child in children)
        assert command[-1] == '--dry-run'
        starts.append(clock.seconds)
        children.append(Child(clock, duration=2, code=1 if not children else 0))
        return children[-1]

    supervisor = service.Supervisor(service.Settings(interval_seconds=30), event,
        process_factory=spawn, heartbeat_writer=lambda _, status: snapshots.append(status.copy()), clock=clock)
    assert supervisor.run() == 0
    assert starts == [0, 32]
    assert all(child.reaped for child in children)
    assert any(status['phase'] == 'idle' and status['last_exit_code'] == 1 for status in snapshots)
    assert any(status['phase'] == 'running' and status['last_exit_code'] == 1 for status in snapshots)
    assert any(status['phase'] == 'idle' and status['last_exit_code'] == 0 for status in snapshots)
    assert snapshots[-1]['phase'] == 'stopped'
    gaps = [b['timestamp'] - a['timestamp'] for a, b in zip(snapshots, snapshots[1:])]
    assert max(gaps) <= service.HEARTBEAT_INTERVAL


@pytest.mark.parametrize('ignore_terminate', [False, True])
def test_signal_stop_reaps_child_and_escalates_only_after_grace(ignore_terminate):
    clock = Clock()
    event = Event(clock, stop_at=3)
    child = Child(clock, duration=1000, ignore_terminate=ignore_terminate)
    snapshots = []
    supervisor = service.Supervisor(service.Settings(), event,
        process_factory=lambda _: child,
        heartbeat_writer=lambda _, status: snapshots.append(status.copy()), clock=clock)
    assert supervisor.run() == 0
    assert child.terminated and child.reaped
    assert child.killed == ignore_terminate
    assert clock.seconds == 3 + (service.STOP_GRACE_SECONDS if ignore_terminate else 0)
    assert snapshots[-1]['phase'] == 'stopped'


def test_heartbeat_failure_stops_child_without_logging_paths(capsys):
    clock = Clock()
    event = Event(clock, stop_at=100)
    child = Child(clock, duration=1000)
    calls = []

    def heartbeat(*args):
        calls.append(args)
        if len(calls) > 1:
            raise OSError('secret-path-must-not-appear')

    supervisor = service.Supervisor(service.Settings(), event,
        process_factory=lambda _: child, heartbeat_writer=heartbeat, clock=clock)
    assert supervisor.run() == 1
    assert child.terminated and child.reaped
    assert 'secret-path-must-not-appear' not in capsys.readouterr().out


def test_healthcheck_command_returns_status_without_starting_worker(tmp_path, monkeypatch):
    heartbeat = tmp_path / 'health.json'
    monkeypatch.setenv('HEARTBEAT_PATH', str(heartbeat))
    monkeypatch.setattr(service, 'Supervisor', lambda *args: pytest.fail('Must not run publisher'))
    assert service.main(['healthcheck']) == 1
    service.write_heartbeat(heartbeat, {'timestamp': service.time.time(), 'pid': 1,
        'phase': 'running', 'last_exit_code': None})
    assert service.main(['healthcheck']) == 0


def test_unknown_command_rejected(capsys):
    assert service.main(['unrecognized']) == 2
    assert 'publisher options' in capsys.readouterr().out
