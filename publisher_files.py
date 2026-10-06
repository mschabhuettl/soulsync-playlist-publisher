"""Read-only planning and independent, exclusive publication of personal copies."""
from __future__ import annotations

import ctypes
from collections.abc import Mapping
import errno
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import unicodedata


AUDIO_EXTENSIONS = frozenset({
    ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aif",
    ".aiff", ".alac", ".ape", ".wv", ".wma", ".dsf", ".dff",
})
_CHUNK = 1024 * 1024


class PublisherFileError(RuntimeError):
    pass


class SourceChangedError(PublisherFileError):
    pass


def _check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise PublisherFileError("File copy deadline exceeded")


def _absolute(value):
    if not isinstance(value, (str, os.PathLike)):
        raise PublisherFileError("An absolute filesystem path is required")
    value = os.fspath(value)
    if not value or "\x00" in value or not os.path.isabs(value):
        raise PublisherFileError("An absolute filesystem path is required")
    return os.path.abspath(value)


def _root(value):
    value = _absolute(value)
    if value == os.path.sep or os.path.realpath(value) != value:
        raise PublisherFileError("Library roots must be absolute, non-symlink folders other than /")
    if os.path.exists(value) and not os.path.isdir(value):
        raise PublisherFileError("Library root is not a directory")
    return value


def _under(path, root):
    return path == root or path.startswith(root + os.sep)


def _allowed_path(path, roots, *, must_exist=True):
    """Require lexical and physical containment within the SAME approved root."""
    path = _absolute(path)
    real = os.path.realpath(path)
    if not any(_under(path, root) and _under(real, _root(root)) for root in roots):
        raise PublisherFileError("Path escapes its allowed library roots")
    if must_exist and not os.path.isfile(real):
        raise PublisherFileError("Audio file does not exist")
    if must_exist and Path(real).suffix.lower() not in AUDIO_EXTENSIONS:
        raise PublisherFileError("File extension is not an allowed audio format")
    return real


def _snapshot(path):
    value = os.stat(path)
    if not stat.S_ISREG(value.st_mode):
        raise PublisherFileError("Only regular audio files can be copied")
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _digest(path, *, deadline=None):
    before = _snapshot(path)
    digest = hashlib.sha256()
    # O_NOFOLLOW closes the final-component symlink race after path validation.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        actual = os.fstat(handle.fileno())
        if (actual.st_dev, actual.st_ino) != before[:2]:
            raise SourceChangedError("Audio file changed while opening it")
        while True:
            _check_deadline(deadline)
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    if _snapshot(path) != before:
        raise SourceChangedError("Audio file changed while hashing it")
    return digest.hexdigest(), before


def _normal(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _duration_ms(value):
    try:
        explicit = float(value.get("duration_ms") or 0)
        return explicit if explicit > 0 else float(value.get("duration") or 0) * 1000
    except (TypeError, ValueError):
        return 0


def _duration_matches(requested, candidate):
    wanted, actual = _duration_ms(requested), _duration_ms(candidate)
    if wanted <= 0:
        return True
    # A short edit can have identical title/artist/album metadata. Unknown
    # candidate length is insufficient evidence when the request has a length.
    return actual > 0 and abs(wanted - actual) <= max(2000, wanted * 0.01)


def _component(value, fallback):
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .") or fallback
    raw = value.encode("utf-8")[:120]
    return raw.decode("utf-8", errors="ignore").rstrip(" .") or fallback


def _song_path(song):
    return song.get("path") or song.get("file_path") or song.get("real_path")


def _base(status, *, source=None, target=None, song_id=None, reason):
    return {"status": status, "source": source, "target": target,
            "song_id": song_id, "reason": reason}


class PreparedInventory(Mapping):
    """One account's inventory with validated paths and lookup indexes."""
    def __init__(self, songs, read_roots, write_root, rows):
        self._songs = songs
        self.read_roots = read_roots
        self.write_root = write_root
        self.by_path = {}
        self.by_metadata = {}
        for row in rows:
            self.by_path.setdefault(row[2], []).append(row)
            key = (_normal(row[3].get("title")), _normal(row[3].get("artist")))
            self.by_metadata.setdefault(key, []).append(row)

    def __getitem__(self, key):
        return self._songs[key]

    def __iter__(self):
        return iter(self._songs)

    def __len__(self):
        return len(self._songs)


def prepare_inventory(inventory, profile, deadline=None):
    """Validate the user's inventory once per run, before planning its tracks."""
    reads = tuple(_root(root) for root in profile["read_roots"])
    write = _root(profile["write_root"])
    if isinstance(inventory, PreparedInventory):
        if inventory.read_roots != reads or inventory.write_root != write:
            raise PublisherFileError("Prepared inventory belongs to a different library profile")
        return inventory
    songs = {str(song_id): dict(song) for song_id, song in inventory.items()}
    rows = []
    for song_id, song in songs.items():
        _check_deadline(deadline)
        try:
            path = _allowed_path(_song_path(song), reads)
        except (PublisherFileError, OSError):
            continue
        rows.append((0 if _under(path, write) else 1, song_id, path, song))
    return PreparedInventory(songs, reads, write, rows)


def plan_track(track, profile, source_roots, inventory, deadline=None):
    """Inspect only: never create directories, files, links or mutable state.

    ``inventory`` must be fetched with the intended user's Navidrome credentials.
    A missing inventory ID is never treated as ready, even if a file exists.
    """
    _check_deadline(deadline)
    reads = tuple(_root(root) for root in profile["read_roots"])
    write = _root(profile["write_root"])
    sources = tuple(_root(root) for root in source_roots)
    if not reads or not sources or not any(_under(write, root) for root in reads):
        raise PublisherFileError("Personal output must be inside an allowed read root")

    prepared = prepare_inventory(inventory, profile, deadline=deadline)

    source_paths = []
    blocked = False
    for candidate in track.get("source_paths") or []:
        try:
            resolved = _allowed_path(candidate, sources)
        except (PublisherFileError, OSError):
            blocked = True
            continue
        if resolved not in source_paths:
            source_paths.append(resolved)

    exact = [row for path in source_paths for row in prepared.by_path.get(path, ())]
    title, artist, album = (_normal(track.get(key)) for key in ("title", "artist", "album"))
    metadata = [row for row in prepared.by_metadata.get((title, artist), ()) if title and artist
                and (not album or _normal(row[3].get("album")) == album)
                and _duration_matches(track, row[3])]
    candidates = exact or metadata
    if candidates:
        rank = min(row[0] for row in candidates)
        candidates = [row for row in candidates if row[0] == rank]
        distinct = {(row[1], row[2]) for row in candidates}
        if len(distinct) != 1:
            return _base("ambiguous", reason="Several accessible tracks match equally; no song selected")
        _, song_id, path, _ = candidates[0]
        try:
            _allowed_path(path, reads)
        except (PublisherFileError, OSError):
            return _base("missing", reason="Previously indexed file is no longer accessible")
        return _base("ready", source=path, target=path, song_id=song_id,
                     reason="Accessible personal-library track" if rank == 0 else "Accessible shared-library track")

    local = [path for path in source_paths if any(_under(path, root) for root in reads)]
    if local:
        local.sort(key=lambda path: (not _under(path, write), path))
        return _base("missing", source=local[0], target=local[0],
                     reason="File exists in an allowed library but is not indexed for this account yet")
    if not source_paths:
        return _base("missing", reason="No usable audio source within approved roots" if blocked
                     else "No source file is available")

    variants = []
    for path in source_paths:
        digest, snapshot = _digest(path, deadline=deadline)
        variants.append((path, digest, snapshot))
    if len({row[1] for row in variants}) > 1:
        return _base("ambiguous", reason="Source files differ; no recording selected automatically")
    source, digest, snapshot = sorted(variants)[0]
    target = os.path.join(write, "_SoulSync", _component(track.get("artist"), "Unknown Artist"),
                          _component(track.get("album"), "Unknown Album"),
                          _component(track.get("title"), "Track") + "__" + digest[:20]
                          + Path(source).suffix.lower())
    target = _allowed_path(target, (write,), must_exist=False)
    plan = _base("copy", source=source, target=target,
                 reason="Independent copy into personal library; waiting for account-specific scan afterward")
    plan["_copy"] = {"sha256": digest, "source_stat": snapshot,
                     "source_roots": sources, "write_root": write}
    return plan


def _publish_exclusive(staged, target):
    """Linux atomic rename without replacement or any hardlink to either file."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise PublisherFileError("Atomic no-replace rename requires Linux renameat2")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(staged), -100, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise FileExistsError(code, os.strerror(code), target)
        raise PublisherFileError(f"Atomic copy publication failed: {os.strerror(code)}")


def install_copy(plan, deadline=None):
    """Install a verified independent copy; never overwrite or delete originals.

    deadline is an absolute time.monotonic() deadline, when supplied. Existing
    targets are accepted only when their complete SHA-256 matches the plan.
    """
    if plan.get("status") != "copy" or not isinstance(plan.get("_copy"), dict):
        raise PublisherFileError("Only a validated copy plan can be installed")
    info = plan["_copy"]
    _check_deadline(deadline)
    write = _root(info["write_root"])
    roots = tuple(_root(root) for root in info["source_roots"])
    source = _allowed_path(plan["source"], roots)
    target = _allowed_path(plan["target"], (write,), must_exist=False)
    if source == target:
        raise PublisherFileError("A copy must have a distinct destination")
    if _snapshot(source) != tuple(info["source_stat"]):
        raise SourceChangedError("Source changed after planning; build a new plan")
    if os.path.lexists(target):
        if _digest(target, deadline=deadline)[0] != info["sha256"]:
            raise PublisherFileError("Destination collision: existing file has different content")
        return target

    parent = os.path.dirname(target)
    _allowed_path(parent, (write,), must_exist=False)
    os.makedirs(parent, exist_ok=True)
    _allowed_path(target, (write,), must_exist=False)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".soulsync-", suffix=".part", dir=parent)
        copied_digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as outgoing:
            source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(source_fd, "rb") as incoming:
                opened = os.fstat(incoming.fileno())
                if (opened.st_dev, opened.st_ino) != tuple(info["source_stat"])[:2]:
                    raise SourceChangedError("Source changed while opening it")
                while True:
                    _check_deadline(deadline)
                    chunk = incoming.read(_CHUNK)
                    if not chunk:
                        break
                    copied_digest.update(chunk)
                    outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
            os.fchmod(outgoing.fileno(), 0o660)
        if _snapshot(source) != tuple(info["source_stat"]) or copied_digest.hexdigest() != info["sha256"]:
            raise SourceChangedError("Source changed during copying; original preserved")
        _check_deadline(deadline)
        _allowed_path(source, roots)
        _allowed_path(target, (write,), must_exist=False)
        try:
            _publish_exclusive(temporary, target)
        except FileExistsError:
            if _digest(target, deadline=deadline)[0] != info["sha256"]:
                raise PublisherFileError("Destination collision: another writer published different content")
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return target
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
