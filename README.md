# SoulSync Playlist Publisher

Zusätzlicher Docker-Container für persönliche Navidrome-Playlists bei einer gemeinsamen SoulSync-Instanz. SoulSync bleibt auf seinem Original-Image und übernimmt weiterhin Spotify-Import, Suche und Downloads. Der Publisher liest SoulSyncs gespeicherte Playlists, kopiert benötigte Musik in den persönlichen Ordner und veröffentlicht die Playlist mit den Track-IDs des jeweiligen Navidrome-Kontos.

Die Einrichtung für einen bestehenden NAS-Stack steht in [README-DE.md](README-DE.md). `compose.yaml` ergänzt genau einen Dienst zum bestehenden Stack.

Die folgenden Namen und Pfade sind Beispiele; sie lassen sich in der Konfiguration anpassen.

| Profil | Wiederverwendung | Ziel zusätzlicher Kopien |
|---|---|---|
| alice | Alice und Shared | `/music/Alice/_SoulSync` |
| bob | Bob und Shared | `/music/Bob/_SoulSync` |

Der Publisher verschiebt oder löscht keine Musik. Liegt ein Lied nur im Ordner der anderen Person, bleibt das Original dort und eine unabhängige Kopie entsteht im eigenen Ordner. Musik aus Shared wird direkt verwendet, wenn das persönliche Navidrome-Konto sie bereitstellt. Erst wenn alle Titel verfügbar sind, wird die private Playlist `SoulSync · <Name>` erstellt oder aktualisiert.

SoulSyncs Bibliotheksanzeige bleibt gemeinsam. Dieses Projekt ergänzt die Dateizuordnung und die persönlichen Navidrome-Playlists; es ergänzt keine native Bibliothekstrennung in SoulSync.

## Container

Das vorgesehene Image heißt `ghcr.io/mschabhuettl/soulsync-playlist-publisher:latest`. Es ist erst nach einem erfolgreichen GitHub-Actions-Lauf verfügbar. Das Repository enthält den vollständigen Build; das Vorhandensein des Dockerfiles allein bedeutet noch keine veröffentlichte Image-Version.

Der Prozess läuft standardmäßig als UID/GID `3007:3007`. Bei einem abweichenden Konto `user:` in Compose anpassen. `PUID` und `PGID` werden vom Zusatzcontainer nicht ausgewertet. Er benötigt keinen Docker-Socket und keinen eingehenden Port.

| Variable | Standard | Bedeutung |
|---|---|---|
| `MODE` | `dry-run` | `dry-run` plant, `apply` kopiert und veröffentlicht |
| `INTERVAL_SECONDS` | `300` | Pause nach einem beendeten Durchlauf, mindestens 30 Sekunden |
| `CONFIG_PATH` | `/config/playlist-publisher.json` | Publisher-Konfiguration |
| `HEARTBEAT_PATH` | `/tmp/publisher-heartbeat.json` | Temporäres Lebenszeichen für den Healthcheck |
| `SOULSYNC_CONFIG_PATH` | `/app/config/config.json` | Bestehende SoulSync-Konfiguration und alter Schlüsselpfad |

Ohne Kommando startet der Container die regelmäßige Ausführung. Publisher-Argumente wie `--list`, `--dry-run` oder `--profile alice --apply` führen genau einen Lauf aus. Ein Einzelaufruf schreibt ausschließlich mit explizitem `--apply`, auch wenn im Dienst `MODE=apply` gesetzt ist.

Der Healthcheck prüft die Aktualität des Lebenszeichens und den letzten Rückgabecode. Ein fehlerhafter Durchlauf bleibt bis zum nächsten erfolgreichen Lauf als unhealthy sichtbar. Fehlende Titel zählen als pending und verhindern die Veröffentlichung ihrer Playlist; dafür den JSON-Bericht beziehungsweise die Logs ansehen. Docker startet einen bloß unhealthy markierten Container nicht automatisch neu; der Scheduler versucht den nächsten Durchlauf selbst.

## Build und Tests

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests
docker build -t soulsync-playlist-publisher:local .
.venv/bin/python tests/container_smoke.py soulsync-playlist-publisher:local
```

Der Container-Smoke-Test benötigt einen laufenden Docker-Daemon. Er verwendet nur temporäre Testdaten und prüft den Zugriff als 3007:3007 auf eine schreibgeschützt gemountete SQLite-Datenbank samt aktivem WAL, die Entschlüsselung vorhandener Profil-Zugänge sowie Dateirechte und Kopien.

GitHub Actions führt die Tests und den Container-Smoke-Test vor der Veröffentlichung aus. Ein Push nach `main` erzeugt `latest` und einen vollständigen Commit-Tag `sha-…`. Tags wie `v0.1.0` erzeugen zusätzlich ein Image-Tag `0.1.0`. Das Image wird für `linux/amd64` und `linux/arm64` gebaut; der Container-Smoke-Test läuft auf amd64. Für ein reproduzierbares Deployment nach dem ersten Build einen Commit-Tag oder den Registry-Digest verwenden.

GHCR verwendet in Actions das automatisch bereitgestellte `GITHUB_TOKEN` mit `packages: write`; es muss kein eigener Upload-Token im Repository gespeichert werden. Sobald das GHCR-Paket nach dem ersten erfolgreichen Build öffentlich freigegeben ist, sind Downloads ohne GitHub-Login möglich. Der Workflow selbst ändert die Sichtbarkeit des Pakets nicht; ein öffentliches Repository allein macht das Container-Paket nicht automatisch öffentlich.

## Grenzen

Das Programm liest SoulSyncs interne Datenbankstruktur. Bei inkompatiblen Änderungen kann ein Update eine Anpassung dieses Adapters erfordern; die Datenbank wird niemals migriert oder beschrieben. Der geprüfte SoulSync-Quellstand ist in `VALIDATION.txt` dokumentiert.

Navidrome muss für die persönlichen Konten echte absolute Dateipfade liefern. Ohne die Einstellung Report Real Path am jeweiligen SoulSync-Player bleibt die Zuordnung gesperrt. Persönliche Konten dürfen nur ihre eigenen und gemeinsamen Bibliotheken sehen.

Neue Dateien benötigen einen Navidrome-Scan. Ein Lauf kann Dateien kopieren und ein späterer Lauf verwendet deren neue persönliche IDs. Unvollständige, leere oder uneindeutige Quellplaylists verändern die vorhandene Zielplaylist nicht. Manuell angelegte Playlists und Playlists ohne die Kennzeichnung dieses Programms werden nicht überschrieben. Kopien und entfernte Playlists werden nicht automatisch aufgeräumt.

Der Publisher startet keine Downloads. Bereits beendete SoulSync-Downloads in `/app/Transfer` können Quellen sein. SABnzbd und Prowlarr werden weiterhin ausschließlich von SoulSync bedient.

Dieses Projekt ist kein offizieller Bestandteil von SoulSync oder Navidrome. Ein Live-Test auf dem NAS mit echten Konten und Musikwiedergabe steht noch aus.
