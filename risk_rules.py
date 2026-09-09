from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WATCHLIST_PATH = Path(__file__).resolve().parent / "watchlist.json"

ORIGINAL_TITLES = {
    "original audio",
    "original sound",
    "originales audio",
    "audio originale",
    "suono originale",
    "originalton",
}

MAJOR_CATALOG_HINTS = (
    "warner",
    "atlantic",
    "universal",
    "umg",
    "sony",
    "columbia",
    "interscope",
    "republic",
    "capitol",
    "b1 recordings",
    "b1 record",
    "goner",
    "in the red",
)

LICENSED_LIBRARIES = (
    "epidemic sound",
    "epidemic",
    "artlist",
    "musicbed",
    "soundstripe",
    "audiojungle",
    "envato",
    "premiumbeat",
    "pond5",
    "storyblocks",
    "motion array",
    "uppbeat",
    "audiio",
)


def norm(text: str | None) -> str:
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", text)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_only.lower()).strip()


def _load_watchlist() -> dict[str, Any]:
    if WATCHLIST_PATH.exists():
        return json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    return {}


def is_original_title(title: str | None, audio_type: str | None = "") -> bool:
    n_title = norm(title)
    n_type = norm(audio_type)
    if n_title in {norm(t) for t in ORIGINAL_TITLES} or n_title == "original":
        return True
    if "original sound" in n_type or n_type == "original":
        return True
    return False


def _blob(*parts: str | None) -> str:
    return " ".join(norm(p) for p in parts if p)


def matches_watchlist(title: str, artist: str, extra: str = "") -> str:
    data = _load_watchlist()
    tracks = data.get("tracks") or []
    aliases: dict[str, list[str]] = data.get("title_aliases") or {}
    hay = _blob(title, artist, extra)
    if not hay:
        return ""

    for track in tracks:
        titles = [track.get("title", "")]
        titles.extend(aliases.get(track.get("title", ""), []))
        title_hit = any(norm(t) and norm(t) in hay for t in titles if t)
        if title_hit:
            return str(track.get("note") or track.get("title"))
    # harte Fallbacks, falls JSON fehlt
    if "love you so" in hay and ("king khan" in hay or "bbq" in hay or "love you so" in norm(title)):
        return "Beobachtungsliste: Love You So (The King Khan & BBQ Show) — bereits 7.000 € Forderung."
    if "victory lap" in hay:
        return "Beobachtungsliste: Victory Lap (Fred again.. / Skepta / PlaqueBoyMax, Atlantic / Warner)."
    return ""


def hits_major_label(*parts: str | None) -> str:
    data = _load_watchlist()
    hints = [norm(h) for h in (data.get("label_hints") or MAJOR_CATALOG_HINTS)]
    text = _blob(*parts)
    for hint in hints:
        if hint and hint in text:
            return hint
    return ""


def hits_licensed_library(*parts: str | None) -> str:
    data = _load_watchlist()
    libs = [norm(h) for h in (data.get("licensed_libraries") or LICENSED_LIBRARIES)]
    text = _blob(*parts)
    for lib in libs:
        if lib and lib in text:
            return lib
    return ""


@dataclass
class RiskDecision:
    risk: str
    reason: str
    action: str


def classify(
    *,
    ig_title: str = "",
    ig_artist: str = "",
    audio_type: str = "",
    fingerprint_title: str = "",
    fingerprint_artist: str = "",
    fingerprint_label: str = "",
    fingerprint_match: bool = False,
) -> RiskDecision:
    """Riskikostufe. Kein Treffer ist niemals LOW / unbedenklich."""
    ig_original = is_original_title(ig_title, audio_type)
    licensed_type = "licensed" in norm(audio_type)
    has_ig_song = bool(norm(ig_title)) and not ig_original
    has_fp = fingerprint_match and bool(norm(fingerprint_title) or norm(fingerprint_artist))

    watch = matches_watchlist(
        fingerprint_title or ig_title,
        fingerprint_artist or ig_artist,
        f"{fingerprint_title} {fingerprint_artist} {ig_title} {ig_artist}",
    )
    if watch:
        return RiskDecision(
            "HIGH",
            watch,
            "mit Anwalt prüfen",
        )

    major = hits_major_label(fingerprint_label, fingerprint_artist, ig_artist, fingerprint_title)
    if major:
        return RiskDecision(
            "HIGH",
            f"Major-Kataloghinweis ({major}) in Label/Künstler. Kommerzielle Nutzung auf einem Geschäftskonto.",
            "stummschalten",
        )

    library = hits_licensed_library(
        fingerprint_label,
        fingerprint_artist,
        fingerprint_title,
        ig_title,
        ig_artist,
    )
    if library:
        return RiskDecision(
            "LOW",
            f"Sieht nach lizenzierter Bibliotheksmusik aus ({library}). Nachweis (Abo/Lizenz) prüfen, dann belassen.",
            "unverändert lassen",
        )

    if licensed_type and has_ig_song:
        return RiskDecision(
            "HIGH",
            "Instagram audio_type=licensed_music mit erkennbarem kommerziellem Titel. IG-Bibliothek ist keine Sync-Lizenz für ein Unternehmenskonto.",
            "stummschalten",
        )

    if has_fp:
        if fingerprint_label:
            return RiskDecision(
                "HIGH",
                f"Fingerprint trifft kommerzielle Aufnahme ({fingerprint_artist} — {fingerprint_title}, Label {fingerprint_label}).",
                "stummschalten",
            )
        return RiskDecision(
            "MEDIUM",
            f"Kommerzieller Titel erkannt ({fingerprint_artist} — {fingerprint_title}), Label unbekannt.",
            "ersetzen",
        )

    if has_ig_song:
        return RiskDecision(
            "MEDIUM",
            "Offizieller Instagram-Sound-Sticker auf einem Geschäftskonto (Bau-Promo = kommerzielle Nutzung).",
            "ersetzen",
        )

    if licensed_type and not ig_original:
        return RiskDecision(
            "MEDIUM",
            "Instagram meldet licensed_music, aber Titel/Interpret unvollständig.",
            "ersetzen",
        )

    return RiskDecision(
        "UNKNOWN",
        "Keine verwertbaren Instagram-Musikmetadaten und kein Fingerprint-Treffer. Nicht als unbedenklich werten.",
        "mit Anwalt prüfen",
    )
