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

- **HIGH** — Beobachtungsliste, Major-Label-Hinweis (Warner, Atlantic, Universal, Sony, …), oder `licensed_music` mit erkennbarem kommerziellem Titel.
- **MEDIUM** — kommerzieller Titel ohne Label, oder offizieller IG-Sound auf dem Geschäftskonto.
- **UNKNOWN** — keine IG-Musikmetadaten und kein Fingerprint. Nicht als sicher bezeichnen.
- **LOW** — klar Bibliotheksmusik (Epidemic, Artlist, …) bzw. nachweislich lizenzierte Nutzmusik.

Beobachtungsliste in `watchlist.json` (erweiterbar):

- The King Khan & BBQ Show — Love You So (bereits beansprucht)
- Fred again.. / Skepta / PlaqueBoyMax — Victory Lap (+ Varianten), Atlantic / Warner Music UK

EU-Kontext: Mandant in Italien (Südtirol). Forderungen kommen oft über Labels oder Verwertungsgesellschaften (SIAE / GEMA-Art) bzw. Kanzleien im Stil SoundGuardian, IPPC Law, Hild & Kollegen, Defend Music. Typisch Tausende Euro **pro Titel**, nicht Instagram-Verwarnungsgebühren.

## Was dieses Repo nicht ist

- kein Massen-Scraper / öffentliches SaaS für beliebige Drittkonten
- kein Login-/Session-Diebstahl, kein Umgehen von Nutzungsbedingungen per Stealth-Browser
- keine Behauptung „keine Übereinstimmung = legal“
- keine anwaltliche Vertretung der 7.000-€-Forderung

## Tests

```bash
python -m pytest
```
