#!/usr/bin/env python3
"""Interner Instagram-Reel-Musikrisiko-Scanner für verwaltete Konten."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from audio import extract_mp3, sha256_file
from audd_client import AcrCloudClient, AuddClient, AuddError, FingerprintHit
from export_csv import build_summary, print_summary, write_csv, write_json
from models import ReelRow, UsageLog
from risk_rules import classify, is_original_title
from scrapecreators_client import (
    ScrapeCreatorsClient,
    ScrapeCreatorsError,
    extract_music,
    music_is_thin,
    parse_handle,
    unwrap_reel_item,
)

ROOT = Path(__file__).resolve().parent
INBOX = ROOT / "inbox"
OUT = ROOT / "out"
AUDIO_DIR = OUT / "audio"
STATE_PATH = OUT / "state.json"

log = logging.getLogger("scanner")


def setup_logging(verbose: bool) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(OUT / "scan.log", encoding="utf-8"),
        ],
    )


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"shortcodes": {}, "hashes": {}}
    return {"shortcodes": {}, "hashes": {}}


def save_state(state: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def row_from_dict(data: dict) -> ReelRow:
    fields = {k: data.get(k, "") for k in ReelRow().__dict__}
    return ReelRow(**fields)


def needs_fingerprint(music: dict[str, str]) -> bool:
    title = music.get("instagram_audio_title") or ""
    audio_type = music.get("audio_type") or ""
    if not title:
        return True
    if is_original_title(title, audio_type):
        return True
    if "licensed" in (audio_type or "").lower():
        return False
    return True


def parse_inbox_name(path: Path) -> tuple[str, str]:
    stem = path.stem
    match = re.match(r"^(?P<handle>[A-Za-z0-9._]+)[_-](?P<code>[A-Za-z0-9_-]+)$", stem)
    if match:
        return match.group("handle"), match.group("code")
    return "", stem


def download_video(url: str, dest: Path, usage: UsageLog) -> Path:
    import requests

    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("Download %s", dest.name)
    resp = requests.get(url, timeout=120, stream=True)
    resp.raise_for_status()
    with dest.open("wb") as handle:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                handle.write(chunk)
    usage.downloads += 1
    return dest


def fingerprint_path(
    video: Path,
    *,
    shortcode: str,
    cheap: bool,
    audd: AuddClient | None,
    acr: AcrCloudClient | None,
    usage: UsageLog,
) -> FingerprintHit:
    mp3 = AUDIO_DIR / f"{shortcode or video.stem}.mp3"
    try:
        extract_mp3(video, mp3, cheap=cheap)
    except Exception as exc:
        usage.errors.append(f"ffmpeg {video.name}: {exc}")
        log.error("ffmpeg fehlgeschlagen: %s", exc)
        return FingerprintHit()

    hit = FingerprintHit()
    if audd is not None:
        try:
            hit = audd.recognize_file(mp3)
        except AuddError as exc:
            usage.errors.append(f"AudD {video.name}: {exc}")
            log.error("%s", exc)
    if not hit.matched and acr is not None:
        try:
            hit = acr.recognize_file(mp3)
        except Exception as exc:
            usage.errors.append(f"ACRCloud {video.name}: {exc}")
            log.error("%s", exc)
    return hit


def apply_risk(row: ReelRow, fp: FingerprintHit | None = None) -> ReelRow:
    if fp is not None and fp.matched:
        row.fingerprint_title = fp.title
        row.fingerprint_artist = fp.artist
        row.fingerprint_label = fp.label
        row.confidence = fp.confidence

    artlist_ok = False
    artlist_note = ""
    title = row.fingerprint_title or row.instagram_audio_title
    artist = row.fingerprint_artist or row.instagram_audio_artist
    if title or artist:
        try:
            from artlist_client import lookup_artlist

            hit = lookup_artlist(title, artist)
            if hit:
                artlist_ok = True
                artlist_note = hit.note or hit.source
                if hit.url and hit.url not in artlist_note:
                    artlist_note = f"{artlist_note}; {hit.url}" if artlist_note else hit.url
        except Exception as exc:  # noqa: BLE001 — Lookup darf Scan nicht abbrechen
            log.warning("Artlist-Lookup fehlgeschlagen: %s", exc)

    decision = classify(
        ig_title=row.instagram_audio_title,
        ig_artist=row.instagram_audio_artist,
        audio_type=row.audio_type,
        fingerprint_title=row.fingerprint_title,
        fingerprint_artist=row.fingerprint_artist,
        fingerprint_label=row.fingerprint_label,
        fingerprint_match=bool(row.fingerprint_title or row.fingerprint_artist),
        artlist_licensable=artlist_ok,
        artlist_note=artlist_note,
    )
    row.risk = decision.risk
    row.risk_reason = decision.reason
    row.action = decision.action
    return row


def scan_handle(
    handle: str,
    *,
    client: ScrapeCreatorsClient,
    audd: AuddClient | None,
    acr: AcrCloudClient | None,
    usage: UsageLog,
    state: dict,
    force: bool,
    cheap: bool,
    metadata_only: bool,
    max_reels: int | None,
    always_fingerprint: bool,
) -> list[ReelRow]:
    rows: list[ReelRow] = []
    items = client.iter_reels(handle, max_reels=max_reels)
    log.info("%s öffentliche Reels gelistet für @%s", len(items), handle)
    hashes: dict[str, str] = state.setdefault("hashes", {})
    done: dict[str, dict] = state.setdefault("shortcodes", {})

    for item in items:
        media = unwrap_reel_item(item)
        music = extract_music(media)
        shortcode = music["shortcode"]
        permalink = music["permalink"] or (
            f"https://www.instagram.com/reel/{shortcode}/" if shortcode else ""
        )
        if not force and shortcode and shortcode in done:
            usage.skipped_shortcodes += 1
            row = row_from_dict(done[shortcode])
            row.skipped = "shortcode"
            rows.append(row)
            log.info("Überspringe %s (bereits gescannt)", shortcode)
            continue

        if music_is_thin(music) and permalink:
            try:
                extra = client.get_post(permalink)
                extra_media = unwrap_reel_item(extra)
                filled = extract_music(extra_media)
                for key, value in filled.items():
                    if value and not music.get(key):
                        music[key] = value
                if filled.get("video_url"):
                    music["video_url"] = filled["video_url"]
            except ScrapeCreatorsError as exc:
                usage.errors.append(f"post {shortcode}: {exc}")
                log.error("%s", exc)

        row = ReelRow(
            permalink=permalink,
            taken_at=music.get("taken_at", ""),
            caption=music.get("caption", ""),
            instagram_audio_title=music.get("instagram_audio_title", ""),
            instagram_audio_artist=music.get("instagram_audio_artist", ""),
            audio_type=music.get("audio_type", ""),
            shortcode=shortcode,
            handle=handle,
            source="scrapecreators",
        )

        fp = FingerprintHit()
        should_fp = always_fingerprint or needs_fingerprint(music)
        if should_fp and not metadata_only:
            dest = INBOX / f"{handle}_{shortcode}.mp4"
            video_path = dest if dest.exists() else None
            if video_path is None and music.get("video_url"):
                try:
                    video_path = download_video(music["video_url"], dest, usage)
                except Exception as exc:
                    usage.errors.append(f"download {shortcode}: {exc}")
                    log.error("Download fehlgeschlagen %s: %s", shortcode, exc)
            if video_path and video_path.exists():
                digest = sha256_file(video_path)
                row.sha256 = digest
                prev = hashes.get(digest)
                if prev and prev != shortcode and not force:
                    usage.skipped_hashes += 1
                    row.skipped = f"sha256:{prev}"
                    log.info("Dedup %s == %s", shortcode, prev)
                else:
                    hashes[digest] = shortcode
                    fp = fingerprint_path(
                        video_path,
                        shortcode=shortcode,
                        cheap=cheap,
                        audd=audd,
                        acr=acr,
                        usage=usage,
                    )
            elif should_fp:
                log.info("Kein lokales Video für %s — Fingerprint übersprungen", shortcode)
        elif should_fp and metadata_only:
            log.info("metadata-only: kein Fingerprint für %s", shortcode)

        apply_risk(row, fp)

        done[shortcode or row.permalink or row.taken_at] = row.to_dict()
        rows.append(row)
        save_state(state)
    return rows


def scan_inbox(
    folder: Path,
    *,
    handle: str,
    audd: AuddClient | None,
    acr: AcrCloudClient | None,
    usage: UsageLog,
    state: dict,
    force: bool,
    cheap: bool,
) -> list[ReelRow]:
    rows: list[ReelRow] = []
    files = sorted(
        [p for p in folder.iterdir() if p.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm"}]
    )
    hashes: dict[str, str] = state.setdefault("hashes", {})
    done: dict[str, dict] = state.setdefault("shortcodes", {})
    for path in files:
        file_handle, shortcode = parse_inbox_name(path)
        handle_use = file_handle or handle or folder.name
        if not force and shortcode in done:
            usage.skipped_shortcodes += 1
            row = row_from_dict(done[shortcode])
            row.skipped = "shortcode"
            rows.append(row)
            continue
        digest = sha256_file(path)
        if digest in hashes and hashes[digest] != shortcode and not force:
            usage.skipped_hashes += 1
            log.info("Dedup Datei %s", path.name)
            continue
        hashes[digest] = shortcode
        fp = fingerprint_path(
            path,
            shortcode=shortcode,
            cheap=cheap,
            audd=audd,
            acr=acr,
            usage=usage,
        )
        row = ReelRow(
            permalink=f"https://www.instagram.com/reel/{shortcode}/" if shortcode else "",
            instagram_audio_title="",
            instagram_audio_artist="",
            audio_type="",
            shortcode=shortcode,
            handle=handle_use,
            source=str(path),
            sha256=digest,
        )
        apply_risk(row, fp)
        done[shortcode or path.name] = row.to_dict()
        rows.append(row)
        save_state(state)
    return rows


def demo_rows() -> list[ReelRow]:
    samples = [
        {
            "permalink": "https://www.instagram.com/reel/demoLoveYouSo/",
            "taken_at": "2025-11-02T08:15:00+00:00",
            "caption": "Baustelle Meran — Demo-Zeile, kein Live-Scan.",
            "instagram_audio_title": "Love You So",
            "instagram_audio_artist": "The King Khan & BBQ Show",
            "audio_type": "licensed_music",
            "shortcode": "demoLoveYouSo",
        },
        {
            "permalink": "https://www.instagram.com/reel/demoVictoryLap/",
            "taken_at": "2026-01-14T10:00:00+00:00",
            "caption": "Rohbau Zeitraffer",
            "instagram_audio_title": "Original audio",
            "instagram_audio_artist": "krapfbau",
            "audio_type": "original_sounds",
            "fp": FingerprintHit(
                title="Victory Lap",
                artist="Fred again..",
                label="Atlantic Records",
                confidence="match",
            ),
            "shortcode": "demoVictoryLap",
        },
        {
            "permalink": "https://www.instagram.com/reel/demoUnknown/",
            "taken_at": "2026-04-01T07:00:00+00:00",
            "caption": "Innenausbau",
            "instagram_audio_title": "Original audio",
            "instagram_audio_artist": "krapfbau",
            "audio_type": "original_sounds",
            "shortcode": "demoUnknown",
        },
        {
            "permalink": "https://www.instagram.com/reel/demoEpidemic/",
            "taken_at": "2026-05-20T09:30:00+00:00",
            "caption": "Projektfilm",
            "instagram_audio_title": "Night Shift",
            "instagram_audio_artist": "Epidemic Sound",
            "audio_type": "licensed_music",
            "shortcode": "demoEpidemic",
        },
        {
            "permalink": "https://www.instagram.com/reel/demoArtlistCadillac/",
            "taken_at": "2026-07-01T11:00:00+00:00",
            "caption": "Außenanlage",
            "instagram_audio_title": "Original audio",
            "instagram_audio_artist": "krapfbau",
            "audio_type": "original_sounds",
            "fp": FingerprintHit(
                title="My New Cadillac",
                artist="Francesco D'Andrea",
                label="Francesco D'Andrea",
                confidence="match",
            ),
            "shortcode": "demoArtlistCadillac",
        },
        {
            "permalink": "https://www.instagram.com/reel/demoIgSound/",
            "taken_at": "2026-06-02T12:00:00+00:00",
            "caption": "Richtfest",
            "instagram_audio_title": "Espresso",
            "instagram_audio_artist": "Sabrina Carpenter",
            "audio_type": "licensed_music",
            "shortcode": "demoIgSound",
        },
    ]
    rows: list[ReelRow] = []
    for sample in samples:
        fp = sample.pop("fp", None)
        row = ReelRow(**sample, handle="krapfbau", source="demo")
        apply_risk(row, fp)
        rows.append(row)
    return rows


def merge_rows(*groups: list[ReelRow]) -> list[ReelRow]:
    by_id: dict[str, ReelRow] = {}
    order: list[str] = []
    for group in groups:
        for row in group:
            key = row.shortcode or row.permalink or row.sha256 or row.taken_at
            if key not in by_id:
                order.append(key)
            by_id[key] = row
    return [by_id[key] for key in order]


def build_clients(usage: UsageLog, args: argparse.Namespace):
    scrape_key = os.getenv("SCRAPECREATORS_API_KEY", "").strip()
    audd_token = os.getenv("AUDD_TOKEN", "").strip() or os.getenv("AUDD_API_TOKEN", "").strip()
    scrape = None
    audd = None
    acr = None
    if scrape_key:
        scrape = ScrapeCreatorsClient(
            scrape_key,
            sleep=args.sleep,
            cache_max_age=args.cache_max_age,
            usage=usage,
        )
    if audd_token:
        audd = AuddClient(audd_token, usage=usage, enterprise=args.enterprise)
    host = os.getenv("ACRCLOUD_HOST", "").strip()
    acr_key = os.getenv("ACRCLOUD_ACCESS_KEY", "").strip()
    acr_secret = os.getenv("ACRCLOUD_ACCESS_SECRET", "").strip()
    if host and acr_key and acr_secret:
        acr = AcrCloudClient(host, acr_key, acr_secret, usage=usage)
    return scrape, audd, acr


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Internes Audit: öffentliche Reels eines verwalteten Instagram-Kontos "
            "auf Musikrisiko prüfen. Nicht als öffentlicher Scanner für fremde Profile nutzen."
        )
    )
    parser.add_argument("--handle", help="Instagram-Handle oder Profil-URL, z. B. krapfbau")
    parser.add_argument("--inbox", help="Ordner mit heruntergeladenen MP4s")
    parser.add_argument("--out", default=str(OUT / "report.csv"), help="CSV-Pfad")
    parser.add_argument("--force", action="store_true", help="Bereits gescannte Shortcodes erneut prüfen")
    parser.add_argument("--cheap", action="store_true", help="Nur die ersten 45 Sekunden an AudD senden")
    parser.add_argument("--sleep", type=float, default=0.5, help="Pause zwischen API-Aufrufen in Sekunden")
    parser.add_argument("--max-reels", type=int, default=None)
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Kein Download / kein Fingerprint, nur Instagram-Sticker",
    )
    parser.add_argument("--always-fingerprint", action="store_true")
    parser.add_argument("--enterprise", action="store_true", help="AudD Enterprise-Endpunkt")
    parser.add_argument("--cache-max-age", default="7d")
    parser.add_argument("--demo", action="store_true", help="Beispieldaten ohne APIs (kein Live-Konto)")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    args = parse_args(argv)
    setup_logging(args.verbose)
    usage = UsageLog()
    state = load_state() if not args.force else {"shortcodes": {}, "hashes": {}}

    out_csv = Path(args.out)
    out_json = out_csv.with_suffix(".json")

    groups: list[list[ReelRow]] = []

    if args.demo:
        log.info("Demo-Modus — keine Live-APIs, keine Aussage über das echte Konto.")
        groups.append(demo_rows())

    handle = parse_handle(args.handle) if args.handle else ""
    scrape, audd, acr = build_clients(usage, args)

    if handle:
        if scrape is None:
            print("SCRAPECREATORS_API_KEY fehlt. Handle-Scan nicht möglich.", file=sys.stderr)
            print("Lege .env an (siehe .env.example) oder nutze --inbox / --demo.", file=sys.stderr)
            if not args.inbox and not args.demo:
                return 2
        else:
            log.info("Scanne @%s (nur verwaltete Konten).", handle)
            groups.append(
                scan_handle(
                    handle,
                    client=scrape,
                    audd=audd,
                    acr=acr,
                    usage=usage,
                    state=state,
                    force=args.force,
                    cheap=args.cheap,
                    metadata_only=args.metadata_only,
                    max_reels=args.max_reels,
                    always_fingerprint=args.always_fingerprint,
                )
            )

    if args.inbox:
        folder = Path(args.inbox)
        if not folder.is_dir():
            print(f"Inbox nicht gefunden: {folder}", file=sys.stderr)
            return 2
        if audd is None and acr is None:
            print("AUDD_TOKEN fehlt. Inbox-Scan ohne Fingerprint ergibt nur UNKNOWN.", file=sys.stderr)
        groups.append(
            scan_inbox(
                folder,
                handle=handle,
                audd=audd,
                acr=acr,
                usage=usage,
                state=state,
                force=args.force,
                cheap=args.cheap,
            )
        )

    if not groups:
        print("Bitte --handle krapfbau, --inbox ./inbox oder --demo angeben.", file=sys.stderr)
        return 2

    rows = merge_rows(*groups)
    save_state(state)
    write_csv(rows, out_csv)
    summary = build_summary(rows, usage)
    write_json(rows, out_json, summary)
    print_summary(summary)
    print(f"\nCSV:  {out_csv}")
    print(f"JSON: {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
