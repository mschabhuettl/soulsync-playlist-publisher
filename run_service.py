#!/usr/bin/env python3
"""Run the standalone publisher periodically, or forward its one-shot CLI.

No arguments: schedule sequential runs, defaulting to dry-run.
Publisher arguments (for example --list): replace this process with one CLI run.
healthcheck: check the scheduler heartbeat and latest completed run's exit code.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

sys.dont_write_bytecode = True

PUBLISHER_SCRIPT = Path(__file__).with_name('publish_playlists.py')
HEARTBEAT_INTERVAL = 10
HEARTBEAT_MAX_AGE = 45
STOP_GRACE_SECONDS = 25


class ServiceError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    mode: str = 'dry-run'
    interval_seconds: int = 300
    config_path: str = '/config/playlist-publisher.json'
    heartbeat_path: str = '/tmp/heartbeat.json'

    @classmethod
    def from_environment(cls, environ=None):
        env = os.environ if environ is None else environ
        mode = env.get('MODE', 'dry-run')
        if mode not in ('dry-run', 'apply'):
            raise ServiceError('MODE must be dry-run or apply')
        try:
            interval = int(env.get('INTERVAL_SECONDS', '300'))
        except (TypeError, ValueError) as exc:
            raise ServiceError('INTERVAL_SECONDS must be an integer from 30 to 86400') from exc
        if not 30 <= interval <= 86400:
            raise ServiceError('INTERVAL_SECONDS must be an integer from 30 to 86400')
        config = env.get('CONFIG_PATH', '/config/playlist-publisher.json')
        heartbeat = env.get('HEARTBEAT_PATH', '/tmp/heartbeat.json')
        if not Path(config).is_absolute() or not Path(heartbeat).is_absolute():
            raise ServiceError('CONFIG_PATH and HEARTBEAT_PATH must be absolute')
        return cls(mode, interval, config, heartbeat)


def publisher_command(settings, arguments=None):
    args = list(arguments) if arguments is not None else ['--' + settings.mode]
    explicit_config = any(arg == '--config' or arg.startswith('--config=') for arg in args)
    config_args = [] if explicit_config else ['--config', settings.config_path]
    return [sys.executable, '-B', str(PUBLISHER_SCRIPT), *config_args, *args]


def write_heartbeat(path, status):
    target = Path(path)
    if target.is_symlink():
        raise ServiceError('Heartbeat file must not be a symlink')
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.publisher-heartbeat-', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(status, handle)
            handle.write('\n')
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def heartbeat_healthy(path, *, now=None):
    try:
        path = Path(path)
        if path.is_symlink():
            return False
        status = json.loads(path.read_text(encoding='utf-8'))
        timestamp = status['timestamp']
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            return False
        age = (time.time() if now is None else now) - timestamp
        return (0 <= age <= HEARTBEAT_MAX_AGE
                and status.get('phase') in ('running', 'idle')
                and status.get('last_exit_code') in (None, 0)
                and isinstance(status.get('pid'), int)
                and status['pid'] > 0)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def log(message):
    print('[publisher-service] ' + message, flush=True)


class Supervisor:
    """A single child is reaped before another can start; no shell is involved."""

    def __init__(self, settings, stop_event=None, *, process_factory=None,
                 heartbeat_writer=None, clock=None):
        self.settings = settings
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.process_factory = process_factory or subprocess.Popen
        self.heartbeat_writer = heartbeat_writer or write_heartbeat
        self.clock = clock or time
        self.child = None
        self.last_exit_code = None
        self.phase = 'idle'
        self.next_heartbeat = 0

    def heartbeat(self, force=False):
        now = self.clock.monotonic()
        if force or now >= self.next_heartbeat:
            self.heartbeat_writer(self.settings.heartbeat_path, {
                'timestamp': self.clock.time(), 'pid': os.getpid(),
                'phase': self.phase, 'mode': self.settings.mode,
                'last_exit_code': self.last_exit_code,
            })
            self.next_heartbeat = now + HEARTBEAT_INTERVAL

    def stop_child(self):
        if self.child is None:
            return
        if self.child.poll() is None:
            log('Stopping the active publisher run')
            self.child.terminate()
            try:
                self.child.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                log('Publisher did not exit during the stop grace period; terminating it')
                self.child.kill()
                self.child.wait()
        else:
            self.child.wait()
        self.child = None

    def run(self):
        log(f'Starting in {self.settings.mode} mode; interval {self.settings.interval_seconds}s')
        try:
            self.heartbeat(force=True)
            while not self.stop_event.is_set():
                self.phase = 'running'
                log('Starting publisher run (' + self.settings.mode + ')')
                self.child = self.process_factory(publisher_command(self.settings))
                self.heartbeat(force=True)
                while self.child.poll() is None and not self.stop_event.is_set():
                    self.heartbeat()
                    self.stop_event.wait(1)
                if self.stop_event.is_set():
                    break
                self.last_exit_code = self.child.wait()
                self.child = None
                self.phase = 'idle'
                log(f'Publisher run finished with exit code {self.last_exit_code}')
                self.heartbeat(force=True)
                next_run = self.clock.monotonic() + self.settings.interval_seconds
                while not self.stop_event.is_set() and self.clock.monotonic() < next_run:
                    self.heartbeat()
                    self.stop_event.wait(min(1, next_run - self.clock.monotonic()))
            return 0
        except (OSError, ValueError) as exc:
            # Avoid echoing filesystem paths or subprocess arguments on errors.
            log('Service stopped because of ' + type(exc).__name__)
            return 1
        finally:
            self.stop_child()
            self.phase = 'stopped'
            try:
                self.heartbeat(force=True)
            except (OSError, ValueError):
                pass
            log('Service stopped')


def main(argv=None):
    # New personal music directories remain writable by the existing arr group.
    # The publisher uses stricter explicit modes for its private state files.
    os.umask(0o002)
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        settings = Settings.from_environment()
        if args == ['healthcheck']:
            return 0 if heartbeat_healthy(settings.heartbeat_path) else 1
        if args:
            if not args[0].startswith('-'):
                raise ServiceError('Use publisher options such as --list, or healthcheck')
            command = publisher_command(settings, args)
            os.execv(command[0], command)
            return 1  # execv only returns in injected test doubles.
        stop_event = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda _signum, _frame: stop_event.set())
        return Supervisor(settings, stop_event).run()
    except ServiceError as exc:
        log(str(exc))
        return 2
    except OSError as exc:
        log('Service could not start: ' + type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
