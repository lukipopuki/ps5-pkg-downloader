# Architektur und Recherche

Diese Datei hält fest, worauf die Implementierung beruht — inklusive der
Stellen, an denen bewusst *nicht* geraten wurde.

## 1. `fetchpkg` (ps5-payload-dev/fetchpkg, GPLv3, C + libcurl)

Gelesen wurden `main.c` und `dl.c`. Der relevante Kern:

* Eingabe ist die URL eines **JSON-Manifests**. `main.c` wandelt vorher
  `_sc.pkg`, `-DP.pkg` und `_0.pkg` in `.json` um.
* `dl_package()` liest `originalFileSize` und das Array `pieces`, öffnet die
  Zieldatei einmal und **hängt die Pieces nacheinander an** — so entsteht aus
  einem gesplitteten Paket eine einzige `.pkg`.
* Pro Piece wird `hashValue` geprüft. Der Algorithmus ergibt sich aus der
  Hex-Länge: 40 Zeichen → SHA-1, 64 → SHA-256. Bei Abweichung gibt fetchpkg nur
  eine Warnung aus.
* Kein Resume, keine Range-Requests, kein Retry, kein Zustand über Neustarts.

Daraus übernommen: Manifest-Format, Piece-Merging, Hash-Auswahl nach Länge,
URL-Umwandlung. Ergänzt: Range-Requests, Resume, Retry mit Backoff, Persistenz,
Abbruch bei Hash-Fehler statt bloßer Warnung.

## 2. Sony-Kette

Verifiziert an echten `version.xml`-Dumps (Repo `1jtp8sobiu/ps5-pkg`):

```xml
<title_patch nptitleid="PPSA03406_00">
  <app_tag content_id="JP0122-PPSA03406_00-SUBNAUTICA000000">
    <package content_ver="01.000.000"
             digest="e84b8e…"                  <!-- SHA-256 der fertigen pkg -->
             manifest_url="https://sgst.prod.dl.playstation.net/sgst/prod/00/
                           PPSA03406_00/app/info/1/f_2b0105…/JP0122-….json"
             system_ver="38797312"              <!-- 0x02500000 → FW 2.50 -->
             delta_url="…-DP.pkg"/>
  </app_tag>
  <ac_tag …>  <!-- DLC -->
</title_patch>
```

* `system_ver` ist BCD-artig. Korrekt dekodiert wird als `%08x`, dann Paare
  lesen: `0x0a010000` → `10.01`. Die naive Dezimal-Variante bricht ab
  Firmware-Major 10 — dagegen gibt es einen Regressionstest.
* Die UUID im `version.xml`-Pfad stammt aus `param.json` (`versionFileUri`) und
  ist **nicht berechenbar**. Deshalb braucht es einen Index oder eine einmalig
  hinterlegte URL.
* `version.xml` beschreibt immer nur den **aktuellen** Patch. Historische
  Stände kennt nur der Index.
* `manifest_url` liegt unter `/app/info/<metadata_ver>/f_<hash>/`, die Pieces
  unter `/app/pkg/<pfs_revision>/f_<anderer hash>/`. Beide Pfade sind
  unabhängig — ein Piece-Link lässt sich deshalb nicht in eine Manifest-URL
  umrechnen.

## 3. PROSPEROPatches

Es gibt keine dokumentierte öffentliche API. Zwei Referenzprojekte:

* **`drakmor/fetchpkg-gui`** bettet WebView2 ein und fängt nur den vom Nutzer
  geklickten offiziellen Link ab — ausdrücklich, weil „PROSPEROPatches does not
  publish a stable public API contract". Von dort stammen auch die Hinweise auf
  HEAD-Ablehnung und die 1-Byte-Range-Probe.
* **`StonedModder/Porkfolio`** spricht die internen Endpunkte an
  (`src/prospero.js`): `GET /{TITLEID}` (HTML mit `id="dynpatch" data-key=…`),
  `POST /api/internal/loadpatches`, `POST /api/internal/loadac`,
  `GET /api/internal/data/switch-region.php`. Der Patch-Datensatz enthält
  `content_ver`, `filesize`, `required_firmware`, `import_date`, `is_latest`
  und ein `keyset` mit den Schlüsseln für die Detail-Modals.

**Wichtig:** `loadpatches` liefert *keine* Manifest-URL. Der Endpunkt hinter
den `keyset`-Schlüsseln konnte nicht verifiziert werden (kein Netzzugriff auf
die Seite in der Entwicklungsumgebung). Deshalb:

* die Kandidaten stehen als Liste in `prospero_rules.yaml` und werden der Reihe
  nach probiert,
* jede Antwort wird generisch nach `*.playstation.net`-Links durchsucht, statt
  auf eine bestimmte JSON-Form zu setzen,
* und es gibt zwei Wege, die ohne diesen Schritt auskommen: Sonys `version.xml`
  und die manuell eingefügte URL.

## 4. CDN-Verhalten

* Die Sony-URLs enthalten Content-Hashes, **keine Signaturen und keine
  Ablaufparameter** — Resume über Container-Neustarts hinweg ist deshalb sicher.
* Wechselt Sony die Revision (neuer Patch), verschwinden die alten Piece-URLs:
  ein laufender Resume läuft dann in 404. Der Job geht auf `error`, der Titel
  muss neu aufgelöst werden.
* `HEAD` wird von manchen Endpunkten abgelehnt. Zuverlässig ist eine
  `Range: bytes=0-0`-Anfrage: `206` plus `Content-Range` liefert Größe und
  Range-Fähigkeit in einem Zug.
* `Accept-Encoding: identity` wird erzwungen, damit Byte-Offsets stimmen.
* Antwortet ein Server auf eine Resume-Anfrage mit `200` statt `206`, wird das
  Piece bei 0 neu begonnen, statt den ganzen Body an den Resume-Offset zu
  schreiben.

## 5. Entscheidungen

| Thema | Entscheidung | Begründung |
|---|---|---|
| Sprache | Python 3.13 + FastAPI | Der Download-Pfad ist reines I/O; der Parser soll leicht anpassbar sein. Ein Go-Binary wäre ~30 MB statt ~160 MB. |
| Frontend | Vanilla JS, kein Build | Kein Node im Image, kein Build-Schritt, kleinere Angriffsfläche. |
| Persistenz | SQLite (WAL) in `/config` | Reicht völlig, ist mit einem Appdata-Backup gesichert. |
| Parallelität | Pieces parallel, innerhalb eines Piece sequentiell | Resume braucht dann nur einen Byte-Offset pro Piece statt einer Lückenverwaltung. |
| Hash bei Resume | Bereits geschriebenes Präfix wird einmal zurückgelesen | Hält die Verifikation über Neustarts hinweg korrekt, ohne Hasher-Zustand zu serialisieren. |
| Zieldatei | Sparse vorallokiert, `pwrite` an absolute Offsets | Pieces können in beliebiger Reihenfolge schreiben; kein Zero-Fill vorab. |
| Fertigstellung | `.part` → `rename` nach Verifikation | Eine `.pkg` im Zielordner ist immer vollständig und geprüft. |
| Systemsoftware | Deny-Liste im Resolver *und* im Downloader | Zwei Stellen, damit auch eine eingefügte URL nicht durchrutscht. |
