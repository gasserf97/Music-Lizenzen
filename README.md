# Musik Lizenzen — Reel-Risiko-Scanner

Internes Audit-Tool für Instagram-Reels von Konten, **die ihr verwaltet**.
Erster Mandant: [krapfbau](https://www.instagram.com/krapfbau/) (Krapf Günther Bau, Südtirol).

Keine öffentliche Website. Kein Scanner für fremde Profile.

## Warum

Ein Reel mit **„Love You So“** (The King Khan & BBQ Show) hat bereits eine **Forderung über 7.000 €** ausgelöst. Der Clip ist gelöscht. Offen: Welche der restlichen Reels können die nächste Mahnung auslösen?

Plattenfirmen hören nicht jedes Video von Hand. Fingerprints gleichen die Wellenform mit dem Katalog ab. Die In-App-Songliste von Instagram ist **keine** kommerzielle Sync-Lizenz für ein Unternehmenskonto.

**Kein Treffer heißt nicht „legal“.** Er heißt nur: Audio nicht identifiziert. Diese Zeilen bleiben **UNKNOWN**.

## Was das Tool liefert

Eine Excel-taugliche CSV plus JSON. Eine Zeile pro Reel:

| Feld | Zweck |
| --- | --- |
| `permalink` | öffnen / stummschalten / löschen |
| `taken_at` | ältere Beiträge werden trotzdem beansprucht |
| `caption` | nur Kontext |
| `instagram_audio_title` / `instagram_audio_artist` | offizieller Sound-Sticker |
| `audio_type` | `licensed_music` vs. Original |
| `fingerprint_*` | AudD (optional ACRCloud) |
| `confidence` | Triage |
| `risk` | HIGH / MEDIUM / LOW / UNKNOWN |
| `risk_reason` | Kurztext |
| `action` | stummschalten / ersetzen / unverändert lassen / mit Anwalt prüfen |

Plus eine Zusammenfassung: Anzahl HIGH, eindeutige Songs, die wie ein Major-Label-Katalog aussehen.

## Voraussetzungen

- Python 3.12
- ffmpeg
- ScrapeCreators-Key (Kunde hat Credits)
- AudD-Token für Original-Audio / Lücken

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # Keys eintragen
```

## Nutzung

Nur Handles, die der Kunde besitzt oder betreut.

```bash
# Live-Scan des Kundenkontos
python scan.py --handle krapfbau --out out/report.csv

# Profil-URL geht auch
python scan.py --handle https://www.instagram.com/krapfbau/ --out out/report.csv

# Bereits heruntergeladene Reels (Dateiname: krapfbau_SHORTCODE.mp4)
python scan.py --inbox ./inbox --out out/report.csv

# Beides, Cache nutzen, Shortcodes überspringen
python scan.py --handle krapfbau --inbox ./inbox --cache-max-age 7d --out out/report.csv

# Günstiger erster Durchlauf: nur 45s Audio, kein Download
python scan.py --handle krapfbau --metadata-only --out out/report.csv
python scan.py --handle krapfbau --cheap --out out/report.csv
```

Ohne Keys (Format prüfen, keine Aussage über das echte Konto):

```bash
python scan.py --demo --out out/report.csv
```

Interne Web-UI (lokal, nicht öffentlich):

```bash
export SCANNER_PASSWORD='ein-langes-passwort'
uvicorn web:app --host 0.0.0.0 --port 8000
# http://127.0.0.1:8000  — Login nur mit Passwort (kein Benutzername)
# /health ohne Login (Render-Check)
```

Erneuter Lauf ist idempotent: bekannte Shortcodes und SHA-256-Duplikate werden übersprungen (`out/state.json`). `--force` scannt neu.

## Ablauf

1. ScrapeCreators listet öffentliche Reels (`GET /v1/instagram/user/reels?handle=…`, Pagination über `max_id`).
2. Musik-Sticker aus `clips_metadata` (title, Interpret, `audio_type`).
3. Ist der Musikblock dünn: `GET /v1/instagram/post?url=…` mit `cache_max_age=7d`.
4. Original / fehlend / verdächtig: Datei nach `inbox/{handle}_{shortcode}.mp4` speichern (CDN-URLs laufen ab).
5. `ffmpeg` → Mono-MP3 44,1 kHz.
6. AudD; optional ACRCloud.
7. Risikoregeln → `out/report.csv` + `out/report.json`.

Der Transkript-Endpunkt von ScrapeCreators wird **nicht** verwendet (Sprache, keine Songs).

## Risikoregeln (v1)

- **HIGH** — bekannte Claims / Watchlist-Tracks (erkannte Titel mit bereits bekannter Abmahnung oder Klage).
- **MEDIUM** — Major-Label-Hinweis (Warner, Atlantic, Universal, Sony, …) oder sonstige kommerzielle Signale ohne bekannten Claim.
- **UNKNOWN** — keine IG-Musikmetadaten und kein Fingerprint. Nicht als sicher bezeichnen.
- **LOW** — Rechte bei Bibliothek, insbesondere **Artlist**, sowie Epidemic Sound u. a. (Nachweis prüfen).
  Fingerprints, die nur den Künstler liefern (z. B. Francesco D'Andrea — *My New Cadillac*), werden über
  `artlist_catalog.json` und optional Live-Suche (`site:artlist.io`) als Artlist-lizenzierbar erkannt → **LOW**.
  Live-Suche: `ARTLIST_LOOKUP=0` zum Abschalten. Cache: `out/artlist_cache.json`.

Beobachtungsliste in `watchlist.json` (erweiterbar) → immer **HIGH**:

- The King Khan & BBQ Show — Love You So (bereits 7.000 € Forderung)
- Fred again.. / Skepta / PlaqueBoyMax — Victory Lap (+ Varianten), Atlantic / Warner Music UK

EU-Kontext: Mandant in Italien (Südtirol). Forderungen kommen oft über Labels oder Verwertungsgesellschaften (SIAE / GEMA-Art) bzw. Kanzleien im Stil SoundGuardian, IPPC Law, Hild & Kollegen, Defend Music. Typisch Tausende Euro **pro Titel**, nicht Instagram-Verwarnungsgebühren.

## Was dieses Repo nicht ist

- kein Massen-Scraper / öffentliches SaaS für beliebige Drittkonten
- kein Login-/Session-Diebstahl, kein Umgehen von Nutzungsbedingungen per Stealth-Browser
- keine Behauptung „keine Übereinstimmung = legal“
- keine anwaltliche Vertretung der 7.000-€-Forderung

## Hosting auf Render

Kleine **interne** Oberfläche, kein öffentliches SaaS. Nur Handles auf der Allowlist
(Standard: `krapfbau`). Fremde Profile werden abgelehnt. Weitere Mandanten kannst du
**in der UI** unter „Kunden / Allowlist“ hinzufügen (oder dauerhaft über `ALLOWED_HANDLES`).

Dieser Cloud-Agent kann **nicht** in ein Render-Konto einloggen und nicht auf „Deploy“ klicken. Ohne `RENDER_API_KEY` in der Umgebung bleibt nur: Repo pushen, dann im [Render-Dashboard](https://dashboard.render.com) verbinden.

### 1. Code auf GitHub

Empfohlenes Repo: [github.com/gasserf97/Music-Lizenzen](https://github.com/gasserf97/Music-Lizenzen).

Wenn das Repo bei dir liegt, dorthin pushen. Danach Render an GitHub anbinden.

### 2. Blueprint (Docker)

1. [dashboard.render.com](https://dashboard.render.com) → **New** → **Blueprint**.
2. GitHub-Repo `gasserf97/Music-Lizenzen` auswählen. Render liest `render.yaml`.
3. Secrets im Dashboard setzen (`sync: false` — nicht in Git):
   - `SCRAPECREATORS_API_KEY`
   - `AUDD_TOKEN` (nur für Fingerprint, nicht für den Metadata-Default)
   - `SCANNER_PASSWORD` — **in Produktion setzen**, sonst ist die UI offen (Login nur Passwort)
   - `ALLOWED_HANDLES` = `krapfbau` (weitere Mandanten komma-getrennt)
4. Deploy. Health-Check: `GET /health`.
5. Start-Kommando (steht im Dockerfile): `uvicorn web:app --host 0.0.0.0 --port $PORT`.

ffmpeg ist im Image (Fingerprint). Standard-Modus der UI ist **nur Metadaten** (kein Download).

### 3. Web Service manuell

**New** → **Web Service** → dasselbe Repo, Runtime **Docker**, Branch mit diesem Code. Health-Pfad `/health`. Dieselben Env-Vars wie oben.

### Hinweise

- Login: nur Passwort (`SCANNER_PASSWORD`), kein Benutzername.
- Abgeschlossene Scans werden unter `out/scans/` gespeichert und in der UI unter „Gespeicherte Scans“ angezeigt.
- Live-Scan ohne `SCRAPECREATORS_API_KEY` geht nicht; Demo-Scan schon.
- Render schließt HTTP-Requests nach ~100 s. Große Live-Scans laufen im Hintergrund; die Seite lädt neu, bis der Report da ist.
- Plan in `render.yaml`: `starter`. Free geht nur, wenn dein Account das noch anbietet — dann im Dashboard umstellen.

## Tests

```bash
python -m pytest
```
