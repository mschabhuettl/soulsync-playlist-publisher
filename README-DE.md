# In den bestehenden SoulSync-Stack einbauen

Der Dienst `soulsync-publisher` läuft neben deiner vorhandenen SoulSync-Instanz. SoulSync behält das Original-Image, seine bisherige Netzwerkadresse und alle bisherigen Mounts. Der neue Container teilt seine Netzwerkverbindung und benötigt keine zusätzliche IP. Beide Dienste müssen deshalb in derselben Compose-App stehen; der vorhandene Dienst muss `soulsync` heißen.

## Verzeichnisse vorbereiten

Auf dem NAS die eigenen Ordner des Publishers erstellen:

```bash
install -d -o 3007 -g 3007 -m 750 \
  /srv/soulsync/publisher/config \
  /srv/soulsync/publisher/state
```

Die Datei `playlist-publisher.example.json` aus diesem Repository nach `publisher/config/playlist-publisher.json` kopieren. Wenn du im heruntergeladenen Repository-Verzeichnis stehst:

```bash
install -o 3007 -g 3007 -m 640 playlist-publisher.example.json \
  /srv/soulsync/publisher/config/playlist-publisher.json
```

Die Vorlage verwendet die Beispielprofile `alice` und `bob` sowie die Ordner Alice, Bob und Shared. Passe die Profilnamen an die tatsächlich in SoulSync vorhandenen Namen und die Ordner an deine Bibliotheken an. Die Hostpfade `/srv/...`, UID/GID `3007:3007` und Zeitzone in `compose.yaml` sind Beispiele und müssen zu deinem System passen. Es werden keine Passwörter eingetragen; der Publisher liest die bereits in SoulSync gespeicherten persönlichen Navidrome-Zugänge und deren vorhandenen Schlüssel. Die Datei bei späteren Updates nicht ungeprüft überschreiben.

## Navidrome prüfen

`alice` muss über seinen gespeicherten Navidrome-Zugang Alice und Shared sehen, `bob` Bob und Shared. Bei den jeweiligen **SoulSync-Playern** in Navidrome **Report Real Path** aktivieren. Falls ein Player noch fehlt, den ersten Probelauf starten, die Einstellung danach setzen und erneut prüfen.

Navidrome, SoulSync und Publisher müssen Musik unter denselben absoluten Pfaden `/music/Alice`, `/music/Bob` und `/music/Shared` sehen. Navidrome muss Dateien mit Eigentümer 3007:3007 und Modus 0660 lesen können. Die bestehenden NAS-Berechtigungen werden nicht automatisch verändert.

## Image beziehen

Das erfolgreich gebaute öffentliche Image ist unter folgendem Namen verfügbar:

```text
ghcr.io/mschabhuettl/soulsync-playlist-publisher:latest
```

Das Image kann ohne GitHub-Login heruntergeladen werden. Der erste Build sowie der anonyme Registry-Zugriff sind geprüft.

## Compose ergänzen

Den Dienst aus `compose.yaml` unter das vorhandene `services:` setzen. Keine zweite SoulSync-App anlegen und keinen zweiten `services:`-Schlüssel einfügen. Den vorhandenen SoulSync-Dienst und seine Netzwerkkonfiguration beibehalten.

Der Zusatzcontainer verwendet `user: "3007:3007"`; SoulSync selbst behält seine bestehenden `PUID`/`PGID`-Einstellungen. `MODE: dry-run` zunächst beibehalten. Die erste Ausführung startet sofort nach dem Containerstart, die weiteren jeweils fünf Minuten nach Abschluss des letzten Durchlaufs.

| Mount im Publisher | Zugriff | Zweck |
|---|---|---|
| `/app/config` | nur lesen | bestehende SoulSync-Einstellungen und gegebenenfalls Schlüssel |
| `/app/data` | nur lesen | gesamte bestehende SQLite-Datenbank samt WAL/SHM und Schlüssel |
| `/app/Transfer` | nur lesen | fertige Downloads als Kopierquelle |
| `/music` | nur lesen | bestehende Musik, einschließlich Shared |
| `/music/Alice` | lesen und schreiben | persönliche Kopien für alice |
| `/music/Bob` | lesen und schreiben | persönliche Kopien für bob |
| `/config` | nur lesen | Publisher-Konfiguration |
| `/state` | lesen und schreiben | eigener Zustand, Sperre und letzter Bericht |

Der gesamte Datenordner muss gemountet werden. Nur `music_library.db` zu mounten würde aktuelle Daten im SQLite-WAL und gegebenenfalls den Schlüssel auslassen. Ein schreibgeschützter Live-Zugriff setzt vorhandene lesbare WAL/SHM-Dateien voraus. Fehlen diese kurz beim Start, wird der Lauf mit Fehler beendet und später erneut versucht. SoulSync weiterlaufen lassen; keine Dateien oder Datenbankeinträge zum Beheben dieses Startzustands löschen.

Ein Docker-Socket, ein SAB-Download-Mount oder Änderungen an SAB-Kategorien sind für den Publisher nicht erforderlich.

Falls bereits eine andere Automation dieses Publishers läuft, diese deaktivieren. Der neue Dienst übernimmt ihre Zeitsteuerung. Spotify-Aktualisierung und SoulSync-Downloads weiterhin aktiviert lassen.

## Probelauf lesen

Nach dem Bereitstellen der ergänzten App:

```bash
docker logs --tail 200 soulsync-publisher
```

`copy` zeigt eine geplante persönliche Kopie; `ready` eine bereits mit dem richtigen Konto nutzbare Datei. `missing`, `ambiguous` oder `blocked` erklären, warum eine Playlist noch nicht vollständig ist. Der Probelauf verändert keine Musik, SoulSync-Datenbank oder Navidrome-Playlist. Nur das temporäre Lebenszeichen für Docker wird geschrieben; der Publisher-Zustand bleibt unverändert.

Quellplaylists samt IDs separat anzeigen:

```bash
docker exec soulsync-publisher python3 -B /opt/publisher/run_service.py --list
```

## Einen Fall anwenden

Eine kleine Testplaylist im Profil alice auswählen, die einen nur bei Bob vorhandenen Titel enthält. Im Befehl `123` durch die angezeigte ID ersetzen:

```bash
docker exec soulsync-publisher python3 -B /opt/publisher/run_service.py \
  --profile alice --playlist-id 123 --apply
```

Zuerst entsteht eine unabhängige Kopie unter `/music/Alice/_SoulSync`. Bobs Original bleibt erhalten. Der Publisher kann mit SoulSyncs bestehendem Servicekonto einen Navidrome-Scan anfordern. Dafür muss dieses Servicekonto Admin sein; die persönlichen Konten brauchen keine Adminrechte. Alternativ den regulären Scan abwarten oder in Navidrome manuell auslösen.

Nach dem Scan denselben Befehl erneut ausführen. Die private Playlist `SoulSync · <Playlistname>` muss bei Alice erscheinen und abspielbar sein. Die Gegenrichtung für bob sowie einen Titel aus Shared ebenfalls prüfen. Solange ein Titel fehlt oder nicht eindeutig passt, wird die gesamte Zielplaylist unverändert gelassen; eine neue unvollständige Playlist wird noch nicht erstellt.

## Regelmäßig anwenden

Nach dem erfolgreichen Test im zusätzlichen Dienst ändern:

```yaml
      MODE: apply
```

Die Compose-App erneut bereitstellen. Der Publisher verarbeitet beide Profile, unabhängig vom gerade in SoulSync ausgewählten Profil. Pro Durchlauf werden höchstens zehn Dateien kopiert. Mit Kopieren, Navidrome-Scan und nächstem Durchlauf können einige Minuten vergehen, bis eine Playlist vollständig verfügbar ist.

Die persönlichen Playlists heißen `SoulSync · <Name>`. SoulSyncs bisherige direkte Navidrome-Playlists und seine globale Trefferanzeige bleiben davon unabhängig. Verwende die vom Publisher erzeugten Playlists zum Prüfen der persönlichen Verfügbarkeit.

## Zustand und Abschalten

Der letzte angewendete Lauf steht auf dem NAS hier:

```text
/srv/soulsync/publisher/state/last-report.json
```

`state.json` daneben enthält die verwalteten Playlist-IDs und Kontozuordnungen, keine Passwörter. Diesen Ordner bei Updates behalten. Temporäre Probleme stehen in den Logs; die Zeitsteuerung versucht es im nächsten Durchlauf erneut.

Zum Anhalten `soulsync-publisher` stoppen oder seinen Dienst aus der Compose-App entfernen. Bereits erstellte Kopien und Playlists bleiben bestehen. Der Container hat keine eigene Weboberfläche.

Wenn SoulSync selbst neu erstellt wird, die Compose-App als Ganzes erneut bereitstellen, damit der Publisher wieder die neue Netzwerkverbindung von SoulSync übernimmt.

Ein echter NAS-Test steht noch aus. Die lokale Testsuite prüft unter anderem persönliche Track-IDs, unabhängige Kopien, unveränderte unvollständige Playlists und den Scheduler. Der GitHub-Build hat zusätzlich den tatsächlichen Container-Test bestanden.
