from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from models import ReelRow, UsageLog
from risk_rules import hits_major_label

CSV_FIELDS = [
    "permalink",
    "taken_at",
    "caption",
    "instagram_audio_title",
    "instagram_audio_artist",
    "audio_type",
    "fingerprint_title",
    "fingerprint_artist",
    "fingerprint_label",
    "confidence",
    "risk",
    "risk_reason",
    "action",
    "shortcode",
    "handle",
    "source",
    "sha256",
    "skipped",
]


def write_csv(rows: list[ReelRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_dict())


def unique_major_songs(rows: list[ReelRow]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    songs: list[dict[str, str]] = []
    for row in rows:
        title = (row.fingerprint_title or row.instagram_audio_title or "").strip()
        artist = (row.fingerprint_artist or row.instagram_audio_artist or "").strip()
        blob = f"{title} {artist} {row.fingerprint_label}"
        if not title:
            continue
        if row.risk != "HIGH" and not hits_major_label(row.fingerprint_label, artist, title):
            continue
        key = (title.lower(), artist.lower())
        if key in seen:
            continue
        seen.add(key)
        songs.append({"title": title, "artist": artist, "label": row.fingerprint_label})
    return songs


def build_summary(rows: list[ReelRow], usage: UsageLog | None = None) -> dict[str, Any]:
    counts = Counter(row.risk for row in rows)
    summary: dict[str, Any] = {
        "total": len(rows),
        "HIGH": counts.get("HIGH", 0),
        "MEDIUM": counts.get("MEDIUM", 0),
        "LOW": counts.get("LOW", 0),
        "UNKNOWN": counts.get("UNKNOWN", 0),
        "unique_major_label_songs": unique_major_songs(rows),
    }
    if usage is not None:
        summary["credits"] = {
            "scrapecreators_live": usage.scrapecreators_live,
            "scrapecreators_cached": usage.scrapecreators_cached,
            "audd_calls": usage.audd_calls,
            "acrcloud_calls": usage.acrcloud_calls,
            "downloads": usage.downloads,
            "skipped_shortcodes": usage.skipped_shortcodes,
            "skipped_hashes": usage.skipped_hashes,
            "errors": usage.errors,
        }
    return summary


def write_json(rows: list[ReelRow], path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"summary": summary, "rows": [row.to_dict() for row in rows]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def print_summary(summary: dict[str, Any], file=sys.stdout) -> None:
    print("\n=== Zusammenfassung ===", file=file)
    print(f"Reels gesamt: {summary.get('total', 0)}", file=file)
    print(f"HIGH:     {summary.get('HIGH', 0)}", file=file)
    print(f"MEDIUM:   {summary.get('MEDIUM', 0)}", file=file)
    print(f"LOW:      {summary.get('LOW', 0)}", file=file)
    print(f"UNKNOWN:  {summary.get('UNKNOWN', 0)}", file=file)
    songs = summary.get("unique_major_label_songs") or []
    print("\nSongs, die wie ein Major-Label-Katalog aussehen:", file=file)
    if not songs:
        print("  (keine in diesem Lauf)", file=file)
    else:
        for song in songs:
            label = f" [{song['label']}]" if song.get("label") else ""
            print(f"  - {song.get('artist', '')} — {song.get('title', '')}{label}", file=file)
    credits = summary.get("credits") or {}
    if credits:
        print("\nAPI-Verbrauch:", file=file)
        print(
            "  ScrapeCreators live={scrapecreators_live} cached={scrapecreators_cached} | "
            "AudD={audd_calls} ACRCloud={acrcloud_calls} | Downloads={downloads} | "
            "übersprungen shortcodes={skipped_shortcodes} hashes={skipped_hashes}".format(
                **{k: credits.get(k, 0) for k in (
                    "scrapecreators_live",
                    "scrapecreators_cached",
                    "audd_calls",
                    "acrcloud_calls",
                    "downloads",
                    "skipped_shortcodes",
                    "skipped_hashes",
                )}
            ),
            file=file,
        )
        errors = credits.get("errors") or []
        if errors:
            print("  Fehler:", file=file)
            for err in errors:
                print(f"    - {err}", file=file)
    print(
        "\nHinweis: UNKNOWN heißt nicht „legal“. Es heißt nur: Audio nicht identifiziert.",
        file=file,
    )
