# PS5 Patch Downloader

Eine WebUI, um PS5-Game-Updates zu suchen und **direkt von den offiziellen
Sony-CDNs** herunterzuladen — als Docker-Container für den Dauerbetrieb auf
Unraid.

Funktional das, was [`fetchpkg`](https://github.com/ps5-payload-dev/fetchpkg)
auf der Kommandozeile macht, aber mit Suche, Warteschlange, Resume,
Fortschrittsanzeige und persistentem Zustand.

> **Nur Game-Updates.** Das Tool lädt oder installiert **keine
> PS5-Systemsoftware** (`PS5UPDATE.PUP`). URLs, die nach Systemsoftware
> aussehen, werden im Resolver *und* im Downloader abgelehnt. Für die
> rechtmäßige Nutzung der heruntergeladenen Updates bist du selbst
> verantwortlich.

---

## Inhalt

- [Was das Tool macht](#was-das-tool-macht)
- [Schnellstart auf Unraid](#schnellstart-auf-unraid)
- [Verzeichnisstruktur auf Unraid](#1-verzeichnisstruktur-auf-unraid)
- [Installation per Docker Compose](#2-installation-per-docker-compose)
  - [Zugriff auf das Repository](#zugriff-auf-das-repository)
- [Port-Mapping](#3-port-mapping)
- [Volume-Mappings](#4-volume-mappings)
- [Environment Variables](#5-environment-variables)
- [Zugriff auf die WebUI](#6-zugriff-auf-die-webui)
- [Backup der Konfiguration](#7-backup-der-konfiguration)
- [Update des Containers](#8-update-des-containers)
- [Wie ein Download aufgelöst wird](#wie-ein-download-aufgelöst-wird)
- [Wenn PROSPEROPatches sich ändert](#wenn-prosperopatches-sich-ändert)
- [API](#api)
- [Dateistruktur der Downloads](#dateistruktur-der-downloads)
- [Troubleshooting](#troubleshooting)
- [Entwicklung](#entwicklung)
- [Lizenz](#lizenz)

---

## Was das Tool macht

* **Suche** nach Title ID (`PPSA08338`) oder Spielname über
  [PROSPEROPatches](https://prosperopatches.com) als Index.
* **Update-Liste** pro Spiel mit Version, benötigter Firmware, Größe und
  Kompatibilitäts-Markierung gegen eine konfigurierbare Maximal-Firmware.
* **Download vom offiziellen Sony-CDN**: das JSON-Manifest wird gelesen, alle
  `pieces` werden geladen und an ihrem `fileOffset` in **eine fertige `.pkg`**
  geschrieben — gesplittete PKGs werden also beim Download zusammengeführt,
  genau wie bei `fetchpkg`.
* **HTTP Range Requests**, Resume nach Container-Neustart, automatische Retries
  mit exponentiellem Backoff, Pause/Fortsetzen/Abbrechen/Wiederholen.
* **Hash-Prüfung**: SHA-256 bzw. SHA-1 pro Piece (der Algorithmus ergibt sich
  aus der Hashlänge im Manifest) und optional SHA-256 über die fertige Datei,
  wenn die `version.xml` einen `digest` liefert.
* **Atomare Fertigstellung**: gearbeitet wird auf `*.pkg.part`, umbenannt wird
  erst nach erfolgreicher Prüfung. Eine `.pkg` im Downloadordner ist also immer
  vollständig und verifiziert.
* **Persistenz in SQLite** unter `/config` — die Queue überlebt Neustarts.

---

## Schnellstart auf Unraid

```bash
mkdir -p /mnt/user/appdata/ps5-patch-downloader
mkdir -p /mnt/user/downloads/PS5
cd /mnt/user/appdata/ps5-patch-downloader
git clone https://github.com/lukipopuki/ps5-pkg-downloader.git src
cd src
docker compose up -d --build
```

WebUI: `http://<unraid-ip>:8080`

Solange das Repository **privat** ist, fragt `git clone` nach Zugangsdaten —
siehe [Zugriff auf das Repository](#zugriff-auf-das-repository). Die
ausführliche Variante steht unten.

---

## 1. Verzeichnisstruktur auf Unraid

```text
/mnt/user/appdata/ps5-patch-downloader/     ← /config (Datenbank, Einstellungen, Regeln)
├── ps5-patch-downloader.sqlite3            ← Queue + Metadaten-Cache
├── prospero_rules.yaml                     ← anpassbare Scraping-Regeln
├── .env                                    ← optional, statt Compose-Environment
└── src/                                    ← optional: das geklonte Repo

/mnt/user/downloads/PS5/                    ← /downloads (fertige Pakete)
└── PPSA08338/
    ├── metadata.json
    └── 01.004.003/
        ├── metadata.json
        └── PPSA08338_01.004.003.pkg
```

Beide Verzeichnisse legst du vorher an (Unraid legt sonst je nach
Share-Einstellung Ordner mit falschen Rechten an):

```bash
mkdir -p /mnt/user/appdata/ps5-patch-downloader /mnt/user/downloads/PS5
chown -R nobody:users /mnt/user/appdata/ps5-patch-downloader /mnt/user/downloads/PS5
```

Der Container braucht **nichts außerhalb dieser beiden Mounts**.

## 2. Installation per Docker Compose

Auf Unraid brauchst du dafür das Plugin **Docker Compose Manager** (Apps →
Plugins), oder du führst `docker compose` per SSH aus.

```bash
cd /mnt/user/appdata/ps5-patch-downloader
git clone https://github.com/lukipopuki/ps5-pkg-downloader.git src
cd src
cp .env.example .env          # optional, Werte anpassen
docker compose up -d --build
docker compose logs -f
```

### Zugriff auf das Repository

Das Repository ist privat, deshalb verlangt `git clone` eine Anmeldung — und
GitHub akzeptiert dafür **kein Account-Passwort** mehr. Drei Wege:

**Personal Access Token.** GitHub → *Settings → Developer settings → Personal
access tokens → Fine-grained tokens → Generate new token*, unter *Repository
access* nur `ps5-pkg-downloader` auswählen und als Berechtigung
*Contents: Read-only* setzen. Beim Klonen dann:

```text
Username: lukipopuki      ← der GitHub-Benutzername, nicht die E-Mail-Adresse
Password: github_pat_…    ← der Token, nicht das Account-Passwort
```

Damit spätere `git pull` den Token nicht erneut abfragen:
`git config credential.helper store` im geklonten Verzeichnis.

**SSH-Key.** Sauberer für einen Server, der dauerhaft läuft:

```bash
ssh-keygen -t ed25519 -C "unraid" -f /root/.ssh/id_ed25519 -N ""
cat /root/.ssh/id_ed25519.pub     # → GitHub, Settings → SSH and GPG keys
git clone git@github.com:lukipopuki/ps5-pkg-downloader.git src
```

Achtung: `/root` liegt auf Unraid im RAM und ist nach einem Neustart weg. Key
dauerhaft sichern und über die `go`-Datei zurückkopieren:

```bash
mkdir -p /boot/config/ssh/root && cp /root/.ssh/id_ed25519* /boot/config/ssh/root/
# in /boot/config/go ergänzen:
# mkdir -p /root/.ssh && cp /boot/config/ssh/root/id_ed25519* /root/.ssh/ \
#   && chmod 600 /root/.ssh/id_ed25519
```

**Ohne Git.** Ein Archiv reicht auch — auf einem Rechner mit GitHub-Zugang
*Code → Download ZIP* laden, per SMB nach
`\\<server>\appdata\ps5-patch-downloader\` kopieren und entpacken:

```bash
cd /mnt/user/appdata/ps5-patch-downloader
unzip ps5-pkg-downloader-*.zip && mv ps5-pkg-downloader-* src && cd src
docker compose up -d --build
```

Dafür gibt es später kein `git pull`; Updates laufen dann über ein neues Archiv.

Wenn dir das alles zu umständlich ist: das Repository unter *Settings →
General → Change visibility* auf öffentlich stellen, dann klont es ohne jede
Anmeldung.

Erwartete erste Zeilen:

```text
2026-09-04 20:14:03  INFO  db                     Database ready  path=/config/ps5-patch-downloader.sqlite3
2026-09-04 20:14:03  INFO  service                Loaded PROSPERO rules  source=/config/prospero_rules.yaml version=1
2026-09-04 20:14:03  INFO  main                   ps5-patch-downloader started  version=1.0.0 port=8080 auth=off
```

Alternativ ohne Compose, direkt über die Unraid-Docker-Oberfläche: die
mitgelieferte **`unraid-template.xml`** nach
`/boot/config/plugins/dockerMan/templates-user/` kopieren, `Repository` und
`Icon` auf dein Image anpassen, dann Docker → *Add Container* → Template
auswählen.

### Ohne Compose, nur mit `docker run`

Setzt voraus, dass ein Image existiert. Bisher ist keines veröffentlicht — es
entsteht beim `docker compose up --build` oben lokal auf dem Server, oder du
baust und pusht selbst eines in eine Registry.

```bash
docker run -d --name ps5-patch-downloader --restart unless-stopped \
  --user 99:100 --stop-timeout 45 \
  -p 8080:8080 \
  -v /mnt/user/appdata/ps5-patch-downloader:/config \
  -v /mnt/user/downloads/PS5:/downloads \
  -e TZ=Europe/Berlin -e MAX_CONCURRENT_DOWNLOADS=2 \
  ghcr.io/lukipopuki/ps5-pkg-downloader:latest
```

## 3. Port-Mapping

| Container | Host | Zweck |
|---|---|---|
| `8080/tcp` | frei wählbar, Standard `8080` | WebUI **und** API |

Es wird **nur dieser eine Port** geöffnet. Standardmäßig ist die WebUI ohne
Authentifizierung erreichbar — halte sie im LAN. Wenn Port 8080 auf deinem
Unraid belegt ist (z. B. durch andere Container), nimm z. B. `-p 8089:8080`.
Willst du sie nur auf eine bestimmte Server-IP binden, schreibe im Compose-File
`- "192.168.1.10:8080:8080"`.

## 4. Volume-Mappings

| Host | Container | Inhalt |
|---|---|---|
| `/mnt/user/appdata/ps5-patch-downloader` | `/config` | SQLite-DB, Einstellungen, `prospero_rules.yaml`, optional `.env` |
| `/mnt/user/downloads/PS5` | `/downloads` | fertige `.pkg`-Dateien, `metadata.json`, laufende `*.part` |

`user: "99:100"` im Compose-File sorgt dafür, dass geschriebene Dateien
`nobody:users` gehören — also genau das, was Unraid für Shares erwartet. Ohne
diese Zeile läuft der Container als UID 1000 aus dem Image.

Lege `/downloads` auf einen Share, der genug Platz hat: ein PS5-Update kann
über 100 GB groß sein. Der Downloader prüft vor dem Start den freien Platz und
bricht mit einer klaren Meldung ab, wenn er nicht reicht.

## 5. Environment Variables

Alles ist optional; die Werte unten sind die Defaults.

### Allgemein

| Variable | Default | Bedeutung |
|---|---|---|
| `TZ` | – | Zeitzone für Log-Zeitstempel, z. B. `Europe/Berlin` |
| `PORT` | `8080` | Port im Container |
| `HOST` | `0.0.0.0` | Listen-Adresse im Container |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FORMAT` | `text` | `text` oder `json` (strukturiert, für Log-Shipper) |
| `CONFIG_DIR` | `/config` | Konfigurationsverzeichnis |
| `DOWNLOAD_DIR` | `/downloads` | Zielverzeichnis |

### Download-Manager

| Variable | Default | Bedeutung |
|---|---|---|
| `MAX_CONCURRENT_DOWNLOADS` | `2` | gleichzeitig laufende Pakete |
| `PIECE_CONCURRENCY` | `2` | parallele Pieces innerhalb eines Split-PKG |
| `MAX_BANDWIDTH_MBPS` | `0` | Gesamt-Limit in Mbit/s, `0` = unbegrenzt |
| `DOWNLOAD_CHUNK_SIZE` | `1048576` | Bytes pro Schreibvorgang |
| `VERIFY_HASHES` | `true` | Piece-Hashes prüfen |
| `PREALLOCATE_FILES` | `true` | Zieldatei sparse vorallokieren |

### Netzwerk

| Variable | Default | Bedeutung |
|---|---|---|
| `HTTP_TIMEOUT_SECONDS` | `60` | Read/Write-Timeout |
| `HTTP_CONNECT_TIMEOUT_SECONDS` | `15` | Verbindungs-Timeout |
| `HTTP_MAX_RETRIES` | `6` | Wiederholungen pro Piece |
| `HTTP_BACKOFF_BASE_SECONDS` | `2` | Basis für exponentielles Backoff |
| `HTTP_BACKOFF_MAX_SECONDS` | `60` | Obergrenze des Backoff |
| `HTTP_PROXY_URL` | – | optionaler Proxy für alle ausgehenden Requests |
| `VERIFY_TLS` | `true` | TLS-Zertifikate prüfen (an lassen) |

### Metadaten

| Variable | Default | Bedeutung |
|---|---|---|
| `CACHE_TTL_HOURS` | `6` | Cache-Dauer für Index-Abfragen |
| `PROSPERO_BASE_URL` | `https://prosperopatches.com` | Basis-URL des Index |
| `PROSPERO_RULES_FILE` | `/config/prospero_rules.yaml` | Regeldatei des Parsers |
| `MAX_FIRMWARE` | – | z. B. `12.60`; leer = kein Filter |

### Sicherheit (alles aus, solange nicht gesetzt)

| Variable | Bedeutung |
|---|---|
| `AUTH_USERNAME` / `AUTH_PASSWORD` | HTTP Basic Auth für WebUI und API |
| `API_TOKEN` | Token für andere Tools (`X-API-Token` oder `Authorization: Bearer`) |
| `READ_ONLY` | `true` = nur Ansicht, keine Downloads, keine Einstellungsänderungen |

Keine Credentials sind im Image hinterlegt, und keine erscheinen im Log.
`/api/health` bleibt bewusst ohne Auth, damit der Docker-Healthcheck
funktioniert.

Eine `.env` wird an zwei Stellen gelesen: von Docker Compose neben dem
Compose-File, und von der Anwendung selbst als `/config/.env`. Werte aus dem
Compose-`environment`-Block gewinnen immer.

## 6. Zugriff auf die WebUI

`http://<unraid-ip>:8080`

* **Suchfeld**: Title ID (`PPSA08338`) oder Name (`Spider-Man 2`).
* **Updates-Tab**: Version, Required FW, Größe, Datum, Kompatibilität, Download-Button.
* **Additional content**: DLC-Pakete desselben Titels.
* **Manual / version.xml**: `version.xml` des Titels registrieren (siehe unten).
* **Start from a link**: direkte Sony-Manifest-URL (`.json`) oder `_sc.pkg`-Link einfügen.
* **Downloads**: Fortschritt, Geschwindigkeit, ETA, geladene/gesamte Bytes,
  Pause / Resume / Cancel / Retry.
* **Settings**: Maximal-Firmware, Cache-TTL, parallele Downloads,
  Bandbreitenlimit, Cache leeren, Regeln neu laden.

Die API-Dokumentation (Swagger) liegt unter `http://<unraid-ip>:8080/api/docs`.

## 7. Backup der Konfiguration

Der komplette Zustand liegt in `/config`:

```bash
# Container kurz stoppen, damit die SQLite-DB konsistent gesichert wird
docker compose stop
tar czf /mnt/user/backups/ps5-patch-downloader-$(date +%F).tar.gz \
    -C /mnt/user/appdata ps5-patch-downloader
docker compose start
```

Ohne Stoppen geht es auch — dann die WAL-Dateien mitnehmen:

```bash
tar czf backup.tar.gz -C /mnt/user/appdata \
    ps5-patch-downloader/ps5-patch-downloader.sqlite3 \
    ps5-patch-downloader/ps5-patch-downloader.sqlite3-wal \
    ps5-patch-downloader/ps5-patch-downloader.sqlite3-shm \
    ps5-patch-downloader/prospero_rules.yaml
```

Das Unraid-Plugin *Appdata Backup* erfasst den Ordner ohnehin automatisch.
Wiederherstellen heißt: Container stoppen, Ordner zurückkopieren, Container
starten — unterbrochene Downloads laufen danach weiter.

## 8. Update des Containers

Bei Compose mit lokalem Build:

```bash
cd /mnt/user/appdata/ps5-patch-downloader/src
git pull
docker compose up -d --build
docker image prune -f
```

Ohne Git (Installation per Archiv): neues Archiv entpacken, `src` ersetzen und
`docker compose up -d --build` erneut ausführen. `/config` und `/downloads`
bleiben dabei unangetastet.

Bei einem fertigen Image:

```bash
docker compose pull
docker compose up -d
```

Über die Unraid-Oberfläche: Docker → *Check for Updates* → *apply update*.

Ein Update ist unkritisch: die Queue liegt in `/config`, laufende Downloads
werden beim Stoppen sauber pausiert (SIGTERM, bis zu 45 s Karenz) und danach
an genau der Byte-Position fortgesetzt. Halbfertige `*.pkg.part`-Dateien
bleiben dafür absichtlich liegen.

---

## Wie ein Download aufgelöst wird

PS5-Updates hängen an einer Kette offizieller Sony-Dokumente:

```text
version.xml   https://sgst.prod.dl.playstation.net/sgst/prod/00/np/PPSA08338_00/<uuid>-version.xml
  └─ <app_tag content_id="EP9000-PPSA08338_00-MARVELSPIDERMAN2">
       └─ <package content_ver="01.004.003" system_ver="167837696"   → Required FW 10.01
                   digest="<sha256 der pkg>"
                   manifest_url="…/app/info/<rev>/f_<hash>/<CONTENT_ID>.json"/>
manifest.json  { "originalFileSize": …, "pieces": [ {url, fileOffset, fileSize, hashValue} ] }
  └─ …/app/pkg/<rev>/f_<hash>/<CONTENT_ID>_0.pkg, _1.pkg, …
```

Um an die `manifest_url` zu kommen, gibt es drei voneinander unabhängige Wege —
die Anwendung probiert sie in dieser Reihenfolge und bleibt benutzbar, wenn
einer davon ausfällt:

1. **Cache** — eine bereits aufgelöste Manifest-URL für genau diese Version.
2. **Sony `version.xml`** — offiziell und ohne Dritte. Sobald die
   `version.xml`-URL eines Titels einmal hinterlegt ist (Tab *Manual /
   version.xml*), liefert Sony selbst Content ID, Version, Required Firmware,
   SHA-256 und Manifest-URL des **aktuellen** Patches. Die UUID im Pfad ist
   nicht berechenbar, deshalb muss sie einmalig bekannt sein.
3. **PROSPEROPatches-Linkauflösung** — regelgesteuert (siehe unten), plus die
   Möglichkeit, die `.json`/`_sc.pkg`-URL eines Updates direkt einzufügen. Das
   funktioniert immer, auch wenn der Index gerade nicht erreichbar ist.

Ältere Patch-Stände beschreibt Sonys `version.xml` nicht (sie kennt nur den
aktuellen) — dafür ist der Index bzw. der eingefügte Link zuständig.

Zwei bewusste Einschränkungen, die aus `fetchpkg` und `fetchpkg-gui` übernommen
sind:

* `…_sc.pkg` → `.json` ist eine exakte Umwandlung (gleiches Verzeichnis,
  gleiche Revision) und wird gemacht.
* `…/app/pkg/…_0.pkg` wird **nicht** in eine JSON-URL umgeschrieben. Das
  Manifest liegt dort unter einer anderen Revision und einem anderen Hash; ein
  geratener Pfad führt zu 404. Solche Links lehnt das Tool mit einer
  entsprechenden Meldung ab.

### Warum keine „richtige" PROSPERO-API?

Weil es keine gibt. PROSPEROPatches veröffentlicht keine dokumentierte,
stabile öffentliche API. `fetchpkg-gui` löst das mit einem eingebetteten
Browser, in dem der Nutzer den Link selbst anklickt; `Porkfolio` spricht die
internen Endpunkte der Website an. Dieses Projekt geht den zweiten Weg,
verlagert aber **jede** URL und **jedes** Regex in eine YAML-Datei, und hat mit
`version.xml` und der manuellen URL zwei Wege, die ohne den Index auskommen.

## Wenn PROSPEROPatches sich ändert

Alles, was der Parser tut, steht in `/config/prospero_rules.yaml`:
Request-Pfade, Request-Methoden, Body-Kodierung und die regulären Ausdrücke für
Titel, Patch-Liste, DLC, Regionen, Suche und Linkauflösung.

```yaml
title_page:
  patterns:
    data_key:
      - 'id="dynpatch"[^>]*data-key="([a-f0-9]+)"'
patches:
  requests:
    - method: POST
      path: '/api/internal/loadpatches'
      encoding: json-body-form-header
      params: { titleid: '{title_id}', key: '{data_key}' }
```

Jede `requests:`-Liste wird der Reihe nach durchprobiert; die erste brauchbare
Antwort gewinnt, Fehlschläge werden protokolliert und übersprungen. Bei der
Linkauflösung wird jede Antwort — HTML wie JSON — generisch nach offiziellen
`*.playstation.net`-Links durchsucht, was die meisten Layout-Änderungen
überlebt.

Ablauf beim Anpassen:

1. Datei in `/mnt/user/appdata/ps5-patch-downloader/prospero_rules.yaml` bearbeiten
   (oder in der WebUI über `POST /api/rules`).
2. In den Settings **Reload rules** klicken bzw. `POST /api/rules/reload`.
3. Fertig — kein Rebuild, kein Neustart.

Ist die Datei kaputt, fällt die Anwendung auf die eingebauten Defaults zurück
und schreibt eine Warnung ins Log.

## API

Backend und Frontend sind getrennt; die WebUI benutzt ausschließlich diese API.

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/api/search?q=spider-man&refresh=false` | Suche nach Name oder Title ID |
| `GET` | `/api/title/PPSA08338` | Titel-Metadaten inkl. Updates, DLC, Regionen |
| `GET` | `/api/title/PPSA08338/updates` | nur die Update-Liste |
| `POST` | `/api/title/PPSA08338/version-xml` | Sony-`version.xml` hinterlegen |
| `GET` | `/api/resolve?title_id=…&content_ver=…` | Manifest-URL auflösen (ohne Download) |
| `POST` | `/api/download` | Download starten |
| `GET` | `/api/downloads` | Queue mit Fortschritt, Speed, ETA |
| `GET` | `/api/download/{id}` | ein Job |
| `POST` | `/api/download/{id}/pause` | pausieren |
| `POST` | `/api/download/{id}/resume` | fortsetzen |
| `POST` | `/api/download/{id}/retry?from_scratch=false` | erneut versuchen |
| `DELETE` | `/api/download/{id}?delete_files=true` | abbrechen/entfernen |
| `GET`/`PUT` | `/api/settings` | Einstellungen lesen/schreiben |
| `POST` | `/api/cache/refresh?title_id=` | Cache leeren bzw. Titel neu laden |
| `GET`/`POST` | `/api/rules`, `/api/rules/reload` | Regeldatei lesen/schreiben/neu laden |
| `GET` | `/api/health` | Healthcheck (immer ohne Auth) |

Beispiele:

```bash
# Suche
curl -s 'http://unraid:8080/api/search?q=spider-man' | jq

# Updates inkl. Firmware-Kompatibilität
curl -s 'http://unraid:8080/api/title/PPSA08338/updates' | jq '.updates[]'

# Download starten (Server löst die Manifest-URL auf)
curl -s -X POST http://unraid:8080/api/download \
     -H 'Content-Type: application/json' \
     -d '{"title_id":"PPSA08338","content_ver":"01.004.003"}'

# Download über eine eingefügte Manifest-URL
curl -s -X POST http://unraid:8080/api/download \
     -H 'Content-Type: application/json' \
     -d '{"manifest_url":"https://sgst.prod.dl.playstation.net/…/CONTENT-ID.json",
          "title_id":"PPSA08338","content_ver":"01.004.003"}'

# Fortschritt
curl -s http://unraid:8080/api/downloads | jq '.downloads[] | {content_ver,progress,speed_bps,eta_seconds}'

# Mit API-Token
curl -s -H "X-API-Token: $API_TOKEN" http://unraid:8080/api/downloads
```

## Dateistruktur der Downloads

```text
/downloads/
└── PPSA08338/
    ├── metadata.json                     ← alle geladenen Versionen dieses Titels
    └── 01.004.003/
        ├── metadata.json                 ← Content ID, FW, Größe, Piece-Hashes, Quelle
        └── PPSA08338_01.004.003.pkg
```

DLC landet unter `PPSA08338/dlc/<CONTENT_ID>/<version>/`.

Während des Downloads existiert `PPSA08338_01.004.003.pkg.part`. Nach
erfolgreicher Prüfung wird atomar umbenannt; nach einem Abbruch bleibt die
`.part`-Datei absichtlich liegen, damit fortgesetzt werden kann. Ein
abgebrochener Job löscht sie (`delete_files=false` behält sie).

## Troubleshooting

**„No results from PROSPEROPatches"**
Der Index ist nicht erreichbar oder hat seine Suche geändert. Suche über die
Title ID (`PPSA08338`) — das geht über einen anderen Pfad — oder passe den
`search:`-Block in `prospero_rules.yaml` an.

**„No official manifest URL could be resolved for this version"**
Es fehlt eine Quelle für die Manifest-URL. Entweder die `version.xml` des
Titels hinterlegen (Tab *Manual / version.xml*, deckt den aktuellen Patch ab)
oder den `.json`/`_sc.pkg`-Link des Updates einfügen.

**„This is a single PS5 package piece below /app/pkg/"**
Der eingefügte Link zeigt auf ein einzelnes Piece. Dessen Manifest liegt unter
einer anderen Revision — nimm den `.json`- oder `_sc.pkg`-Link des Updates.

**Download bleibt bei „error" mit HTTP 404**
Sony hat die Revision gewechselt: die alten Piece-URLs verschwinden, wenn ein
neuer Patch erscheint. Titel neu laden (*Refresh*) und den Download neu
anlegen.

**„not enough free space"**
Die Prüfung läuft gegen den Share hinter `/downloads`. Platz schaffen oder das
Volume auf einen größeren Share legen.

**Hash-Mismatch im Log**
Das betroffene Piece wird genau einmal komplett neu geladen. Bleibt es
inkonsistent, geht der Job auf `error`, statt eine kaputte `.pkg` zu
veröffentlichen.

**Falsche Dateirechte auf Unraid**
`user: "99:100"` im Compose-File bzw. `--user 99:100` bei `docker run`.

## Entwicklung

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt

# Tests (Unit + Integration; die Download-Tests laufen gegen einen echten
# lokalen HTTP-Server mit Range-Support)
.venv/bin/python -m pytest -q

# Lokal starten
CONFIG_DIR=./data/config DOWNLOAD_DIR=./data/downloads \
  .venv/bin/python -m app.main   # aus dem Ordner backend/
```

Aufbau:

```text
backend/app/
├── main.py            FastAPI-App, Lifespan, Signal-Handling
├── config.py          Environment-Konfiguration
├── service.py         Cache, Auflösungskette, Einstiegspunkte
├── db.py              SQLite-Schema und Zugriffe
├── security.py        optionale Basic Auth / API-Token
├── versions.py        Firmware- und Versionslogik
├── api/               HTTP-Routen und Schemas
├── providers/
│   ├── sony.py        version.xml + Manifest-Parser, URL-Regeln (rein)
│   ├── sony_client.py HTTP-Zugriff auf Sony
│   ├── prospero.py    Index-Client + Parser (rein testbar)
│   ├── rules.py       Laden/Validieren der Regeldatei
│   └── prospero_rules.default.yaml
└── download/
    ├── engine.py      Range-Transfer, Resume, Hashing
    ├── manager.py     Queue, Persistenz, Zustandsübergänge
    ├── storage.py     Pfade, atomares Umbenennen, metadata.json
    └── ratelimit.py   Token-Bucket, Speed/ETA
```

## Rechtliches

Dieses Projekt lädt ausschließlich **Game-Update-Pakete** von den offiziellen
Sony-Servern. Es umgeht keinen DRM- oder Lizenzschutz, entschlüsselt nichts,
meldet sich nicht am PlayStation Network an, verändert keine Systemsoftware und
lädt keine `PS5UPDATE.PUP`. Es stellt keine Spiele aus Drittquellen bereit.
Ob du die geladenen Updates nutzen darfst, hängt davon ab, ob du das jeweilige
Spiel besitzt — dafür bist du verantwortlich.

Nicht verbunden mit Sony Interactive Entertainment oder PROSPEROPatches.

## Lizenz

GPL-3.0-or-later, passend zur Lizenz von `fetchpkg`, dessen Manifest-Verarbeitung
hier nachgebaut wurde. Siehe [LICENSE](LICENSE).
