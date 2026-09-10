"""Erkennung von Tracks, die über Artlist lizenzierbar sind.

AudD/ACR liefern oft nur Titel/Interpret (z. B. Francesco D'Andrea), nicht den
Hinweis „Artlist“. Dieser Client prüft deshalb:

1. lokalen Katalog ``artlist_catalog.json`` (Titel+Interpret oder bekannter Artlist-Künstler)
2. Disk-Cache früherer Lookups
3. optional Live-Suche ``site:artlist.io/royalty-free-music/song`` (DuckDuckGo HTML)

Enterprise-API-Credentials sind optional; ohne sie bleibt Katalog + Suche aktiv.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from risk_rules import norm

log = logging.getLogger("scanner.artlist")

ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / "artlist_catalog.json"
DEFAULT_CACHE_PATH = ROOT / "out" / "artlist_cache.json"

SONG_URL_RE = re.compile(
    r"https?://(?:www\.)?artlist\.io/royalty-free-music/song/([a-z0-9-]+)/\d+",
    re.I,
)


@dataclass
class ArtlistHit:
    title: str
    artist: str
    url: str = ""
    source: str = ""
    note: str = ""


def _cache_path() -> Path:
    raw = (os.getenv("ARTLIST_CACHE_PATH") or "").strip()
    return Path(raw) if raw else DEFAULT_CACHE_PATH


def lookup_enabled() -> bool:
    value = (os.getenv("ARTLIST_LOOKUP") or "1").strip().lower()
    return value not in {"0", "false", "off", "no", "nein"}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(cache: dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _cache_key(title: str, artist: str) -> str:
    return f"{norm(title)}|{norm(artist)}"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(text)).strip("-")


def _title_matches(expected: str, candidate: str) -> bool:
    a, b = norm(expected), norm(candidate)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _artist_matches(expected: str, aliases: list[str], candidate: str) -> bool:
    cand = norm(candidate)
    if not cand:
        return False
    names = [expected, *aliases]
    for name in names:
        n = norm(name)
        if n and (n == cand or n in cand or cand in n):
            return True
    return False


def match_catalog(title: str, artist: str, *, catalog: dict[str, Any] | None = None) -> ArtlistHit | None:
    data = catalog if catalog is not None else _load_json(CATALOG_PATH)
    for track in data.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        t_title = str(track.get("title") or "")
        artists = [str(a) for a in (track.get("artists") or []) if a]
        if not _title_matches(title, t_title):
            continue
        if artists and norm(artist) and not any(_artist_matches(a, [], artist) for a in artists):
            continue
        return ArtlistHit(
            title=t_title or title,
            artist=(artists[0] if artists else artist),
            url=str(track.get("url") or ""),
            source="catalog",
            note=str(track.get("note") or "Artlist-Katalog"),
        )

    # Bekannte Artlist-Künstler: Fingerprint-Interpret reicht für LOW
    for entry in data.get("artists") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        aliases = [str(a) for a in (entry.get("aliases") or []) if a]
        if not _artist_matches(name, aliases, artist):
            continue
        return ArtlistHit(
            title=title,
            artist=name or artist,
            url=str(entry.get("url") or ""),
            source="catalog_artist",
            note=str(entry.get("note") or "Artlist-Künstler"),
        )
    return None


def _parse_ddg_results(html: str) -> list[tuple[str, str]]:
    """Return (url, snippet) pairs from DuckDuckGo HTML."""
    results: list[tuple[str, str]] = []
    # uddg redirect links
    for match in re.finditer(r'uddg=([^&"]+)', html):
        url = urllib.parse.unquote(match.group(1))
        if "artlist.io/royalty-free-music/song/" in url:
            results.append((url.split("&")[0], ""))
    for match in SONG_URL_RE.finditer(html):
        results.append((match.group(0), ""))
    # result blocks
    for block in re.finditer(r'class="result[^"]*"[^>]*>(.*?)</div>\s*</div>', html, re.I | re.S):
        chunk = block.group(1)
        urls = SONG_URL_RE.findall(chunk)
        if not urls:
            uddg = re.search(r'uddg=([^&"]+)', chunk)
            if uddg:
                decoded = urllib.parse.unquote(uddg.group(1))
                m = SONG_URL_RE.search(decoded)
                if m:
                    results.append((m.group(0), re.sub(r"<[^>]+>", " ", chunk)))
            continue
        snippet = re.sub(r"<[^>]+>", " ", chunk)
        results.append((f"https://artlist.io/royalty-free-music/song/{urls[0]}/0", snippet))
    # de-dupe by song slug
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for url, snip in results:
        m = SONG_URL_RE.search(url)
        key = m.group(1) if m else url
        if key in seen:
            continue
        seen.add(key)
        unique.append((url, snip))
    return unique


def _live_search(title: str, artist: str, *, timeout: float = 12.0) -> ArtlistHit | None:
    if not title:
        return None
    query = f'site:artlist.io/royalty-free-music/song "{title}"'
    if artist:
        query += f' "{artist}"'
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "MusikLizenzenScanner/1.0 (+internal audit)"},
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Artlist-Live-Suche fehlgeschlagen: %s", exc)
        return None

    want_slug = slugify(title)
    artist_n = norm(artist)
    for url, snippet in _parse_ddg_results(resp.text):
        m = SONG_URL_RE.search(url)
        if not m:
            continue
        slug = m.group(1).lower()
        blob = norm(f"{slug.replace('-', ' ')} {snippet} {url}")
        slug_ok = want_slug and (want_slug == slug or want_slug in slug or slug in want_slug)
        title_ok = slug_ok or (norm(title) and norm(title) in blob)
        artist_ok = not artist_n or artist_n in blob or any(
            part and part in blob for part in artist_n.split() if len(part) > 2
        )
        if title_ok and artist_ok:
            clean = m.group(0)
            return ArtlistHit(
                title=title,
                artist=artist,
                url=clean,
                source="live_search",
                note="Treffer in Artlist-Katalogsuche (Song-URL)",
            )
    return None


def lookup_artlist(
    title: str,
    artist: str,
    *,
    allow_network: bool | None = None,
    catalog: dict[str, Any] | None = None,
) -> ArtlistHit | None:
    """Prüft, ob Titel/Interpret über Artlist lizenzierbar ist."""
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title and not artist:
        return None

    hit = match_catalog(title, artist, catalog=catalog)
    if hit:
        return hit

    key = _cache_key(title, artist)
    cache = _load_json(_cache_path())
    entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    cached = entries.get(key)
    if isinstance(cached, dict):
        if cached.get("miss"):
            return None
        if cached.get("url") or cached.get("source"):
            return ArtlistHit(
                title=str(cached.get("title") or title),
                artist=str(cached.get("artist") or artist),
                url=str(cached.get("url") or ""),
                source=str(cached.get("source") or "cache"),
                note=str(cached.get("note") or "Artlist-Cache"),
            )

    use_network = lookup_enabled() if allow_network is None else allow_network
    if not use_network or not title:
        return None

    live = _live_search(title, artist)
    if live:
        entries[key] = {
            "title": live.title,
            "artist": live.artist,
            "url": live.url,
            "source": live.source,
            "note": live.note,
            "miss": False,
        }
        cache["entries"] = entries
        _save_cache(cache)
        return live

    entries[key] = {"miss": True, "title": title, "artist": artist}
    cache["entries"] = entries
    _save_cache(cache)
    return None
