# SoulSync Playlist Publisher

[Deutsche Installationsanleitung](README-DE.md)

A Docker sidecar that creates personal Navidrome playlists alongside a shared SoulSync instance. SoulSync keeps its original image and handles Spotify imports, searches, and downloads. The publisher reads SoulSync's saved playlists, copies music into each user's library when needed, and publishes playlists using that user's Navidrome track IDs.

## How it works

The example configuration defines two profiles and one shared library. Adapt these names and paths to your installation.

| SoulSync profile | Can reuse tracks from | Destination for personal copies |
|---|---|---|
| `alice` | Alice and Shared | `/music/Alice/_SoulSync` |
| `bob` | Bob and Shared | `/music/Bob/_SoulSync` |

If a track exists only in Bob's library but Alice needs it, the publisher creates an independent copy in Alice's library. The original stays in place. Tracks in Shared are reused when the personal Navidrome account can access them.

The publisher does not move, delete, overwrite, or hard-link music files. It creates or updates a private playlist named `SoulSync · <Name>` only when every source track is available and matched unambiguously.

SoulSync's library view and matching state remain shared. This project handles personal copies and Navidrome playlists afterward; it does not add native library isolation to SoulSync.

## Container image

```text
ghcr.io/mschabhuettl/soulsync-playlist-publisher:latest
```

The image is public and available for `linux/amd64` and `linux/arm64`. No GitHub login is required to pull it. The first build passed 145 tests and an actual container integration test. Anonymous registry access was verified.

The container runs as UID/GID `3007:3007` by default. Change `user:` in Compose if your media files use different ownership. This image does not interpret `PUID` or `PGID`. It needs no Docker socket, web interface, or published port.

## Installation

### Prepare the configuration

Clone or download this repository. The commands below use the sample host paths from [compose.yaml](compose.yaml); adapt them and the UID/GID before running them. Run the directory and file installation commands with sufficient privileges on the Docker host.

```bash
install -d -o 3007 -g 3007 -m 750 \
  /srv/soulsync/publisher/config \
  /srv/soulsync/publisher/state

install -o 3007 -g 3007 -m 640 playlist-publisher.example.json \
  /srv/soulsync/publisher/config/playlist-publisher.json
```

Edit the installed JSON file so the profile names exactly match your SoulSync profiles. Set each profile's `read_roots` and `write_root`, and adapt `source_roots` to the existing music and completed-download directories. Each personal output folder must be separate from the other user's accessible roots.

No passwords need to be added to this file. The publisher reads the personal Navidrome credentials already stored by SoulSync and decrypts them using SoulSync's existing encryption key. Keep your configuration when upgrading.

### Check Navidrome access

Each SoulSync profile must have its own Navidrome credentials. In this example, Alice's account can access Alice and Shared, while Bob's account can access Bob and Shared.

Enable **Report Real Path** for each account's **SoulSync player** in Navidrome. If the player does not exist yet, run the publisher once, enable the setting, and retry.

Navidrome, SoulSync, and the publisher must see music at the same absolute paths, such as `/music/Alice`. Navidrome must be able to read the copied files: their owner is the container's UID/GID and their mode is `0660`. The publisher does not change permissions on existing music.

### Add the service to your existing stack

Merge the `soulsync-publisher` service from [compose.yaml](compose.yaml) under the existing stack's `services:` key. Keep your current SoulSync service and network configuration. Adapt the sample `/srv/...` host paths, music folder names, UID/GID, and timezone.

The example expects the existing service to be named `soulsync`. Both services must be in the same Compose project because `network_mode: service:soulsync` shares SoulSync's network namespace. The publisher does not need another IP address.

| Container path | Access | Purpose |
|---|---|---|
| `/app/config` | Read-only | Existing SoulSync settings and possible legacy encryption key |
| `/app/data` | Read-only | Entire SoulSync data directory, including SQLite WAL/SHM and encryption key |
| `/app/Transfer` | Read-only | Completed downloads used as copy sources |
| `/music` | Read-only | Existing music, including the shared library |
| `/music/Alice`, `/music/Bob` | Read/write | Personal copies |
| `/config` | Read-only | Publisher configuration |
| `/state` | Read/write | Publisher state, lock, and last report |

Mount the entire SoulSync data directory, not just `music_library.db`. Current changes may still be in its WAL, and the encryption key may be in the same directory. Read-only access to a live WAL database requires readable WAL/SHM files. If they are temporarily unavailable during startup, the run fails and the scheduler retries later. Keep SoulSync running; do not delete database files to resolve this condition.

Keep `MODE: dry-run` for the first deployment. If you previously installed the publisher as a SoulSync **Run Script** automation, disable that automation: the sidecar provides its own schedule. SoulSync's Spotify refresh and download jobs should continue running.

### Inspect the dry run

Deploy the updated stack, then read the container logs:

```bash
docker logs --tail 200 soulsync-publisher
```

`copy` indicates a planned personal copy; `ready` means the correct account can already access the file. `missing`, `ambiguous`, and `blocked` explain why a playlist is not ready.

A dry run does not change music, SoulSync's database, Navidrome playlists, or persistent publisher state. The scheduler writes only a temporary heartbeat for Docker's healthcheck.

List the source playlists and their IDs:

```bash
docker exec soulsync-publisher python3 -B /opt/publisher/run_service.py --list
```

### Test one playlist

Choose a small playlist owned by Alice that includes a track available only in Bob's library. Replace `123` with the source playlist ID:

```bash
docker exec soulsync-publisher python3 -B /opt/publisher/run_service.py \
  --profile alice --playlist-id 123 --apply
```

The first run should create a copy under `/music/Alice/_SoulSync`. After Navidrome has scanned the new file, repeat the command. The private `SoulSync · <Name>` playlist should appear in Alice's account and be playable there. Also check the reverse direction and a track from Shared.

The publisher can request a scan through SoulSync's existing service account if that account is a Navidrome administrator. Personal accounts do not need administrator permissions. Otherwise, wait for Navidrome's regular scan or start one manually.

### Enable automatic processing

After the single-playlist test succeeds, change the publisher service to:

```yaml
environment:
  MODE: apply
```

Preserve its other environment settings and redeploy the stack. The publisher processes all configured profiles independently of the profile currently selected in SoulSync. By default it copies at most ten files per run and waits five minutes after each completed run before starting the next one.

A playlist may need several runs: copy files, wait for a Navidrome scan, then publish using the new personal track IDs. Until all tracks are ready, its existing destination playlist remains unchanged.

## Runtime settings

| Variable | Default | Meaning |
|---|---|---|
| `MODE` | `dry-run` | `dry-run` plans changes; `apply` copies files and publishes playlists |
| `INTERVAL_SECONDS` | `300` | Delay after each completed run; minimum 30 seconds |
| `CONFIG_PATH` | `/config/playlist-publisher.json` | Publisher configuration file |
| `HEARTBEAT_PATH` | `/tmp/publisher-heartbeat.json` | Temporary scheduler heartbeat |
| `SOULSYNC_CONFIG_PATH` | `/app/config/config.json` | Existing SoulSync configuration and legacy key location |

With no command, the container starts the scheduler. Arguments such as `--list`, `--dry-run`, or `--profile alice --apply` perform one run. A one-shot invocation writes only when explicitly passed `--apply`, even if the service environment contains `MODE=apply`.

The healthcheck checks heartbeat freshness and the last completed run's exit code. A failed run remains unhealthy until a successful retry. Missing tracks are reported as pending and prevent publication of their playlist; check the JSON report or logs for details. Docker does not automatically restart a container simply because it is unhealthy; the scheduler handles retries.

## State and shutdown

The latest applied run is recorded in `/state/last-report.json`. With the sample mounts, its host path is `/srv/soulsync/publisher/state/last-report.json`.

`state.json` stores managed playlist IDs and account mappings, without passwords. Preserve the state directory across upgrades.

Stop or remove the `soulsync-publisher` service to disable it. Existing copies and playlists remain. If SoulSync itself is recreated, redeploy the Compose stack together so the publisher uses SoulSync's new network namespace.

## Building and testing

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests
docker build -t soulsync-playlist-publisher:local .
.venv/bin/python tests/container_smoke.py soulsync-playlist-publisher:local
```

The container integration test requires a running Docker daemon. It uses disposable fixtures to verify UID/GID `3007:3007`, a read-only SQLite source with a live WAL, existing credential decryption, file permissions, and independent copies.

GitHub Actions runs the tests before publishing. A normal push to `main` produces `latest` and a full commit tag `sha-…`. Version tags such as `v0.1.0` additionally produce `0.1.0`. Images are built for AMD64 and ARM64; the actual container integration test runs on AMD64. Pin a commit tag or registry digest to keep deployments on a specific build.

The workflow uses GitHub's built-in `GITHUB_TOKEN` with `packages: write`; no personal upload token is needed. This repository's GHCR package is public. If you fork the project, check package visibility separately because the workflow does not change it.

## Limitations

The publisher reads SoulSync's internal database schema. An incompatible SoulSync update may require an adapter update. It never migrates or writes to the source database. The tested upstream revision and validation results are recorded in [VALIDATION.txt](VALIDATION.txt).

Empty, incomplete, or ambiguous source playlists do not change their existing destinations. New incomplete playlists are not published. Manual playlists and playlists without this publisher's ownership marker are not overwritten. Removed playlists and unused copies are not automatically deleted.

The publisher does not initiate downloads. Completed SoulSync downloads in `/app/Transfer` can be used as sources. SoulSync continues to handle SABnzbd and Prowlarr.

This is an independent project, not an official component of SoulSync or Navidrome. Testing with live NAS permissions, real Navidrome accounts, scans, and audio playback is still required.
