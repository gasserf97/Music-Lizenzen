from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import requests

log = logging.getLogger("scanner.scrapecreators")

BASE = "https://api.scrapecreators.com"


class ScrapeCreatorsError(RuntimeError):
    pass


class ScrapeCreatorsClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout: int = 60,
        sleep: float = 0.5,
        cache_max_age: str = "7d",
        usage: Any | None = None,
    ) -> None:
        if not api_key:
            raise ScrapeCreatorsError("SCRAPECREATORS_API_KEY fehlt.")
        self.api_key = api_key
        self.timeout = timeout
        self.sleep = sleep
        self.cache_max_age = cache_max_age
        self.usage = usage
        self._last_call = 0.0

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "Accept": "application/json"}

    def _pause(self) -> None:
        elapsed = time.time() - self._last_call
        if elapsed < self.sleep:
            time.sleep(self.sleep - elapsed)
        self._last_call = time.time()

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self._pause()
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        url = f"{BASE}{path}"
        log.info("GET %s %s", path, {k: v for k, v in params.items() if k != "url"} or params)
        try:
            resp = requests.get(url, headers=self._headers(), params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ScrapeCreatorsError(f"Netzwerkfehler {path}: {exc}") from exc

        if resp.status_code == 401:
            raise ScrapeCreatorsError("ScrapeCreators: 401 — API-Key prüfen.")
        if resp.status_code == 402:
            raise ScrapeCreatorsError("ScrapeCreators: 402 — Credits aufgebraucht.")
        if resp.status_code >= 400:
            raise ScrapeCreatorsError(f"ScrapeCreators {resp.status_code}: {resp.text[:400]}")

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise ScrapeCreatorsError(f"Ungültiges JSON von {path}") from exc

        cached = bool(data.get("cached")) if isinstance(data, dict) else False
        if self.usage is not None:
            self.usage.note_scrape(cached)
        if cached:
            log.info("Cache-Treffer (%s credits=0)", data.get("cached_at"))
        return data if isinstance(data, dict) else {"data": data}

    def list_reels_page(self, handle: str, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "handle": handle,
            "cache_max_age": self.cache_max_age,
        }
        # API nutzt max_id; Briefing erwähnt cursor — beide mitschicken, falls gesetzt
        if cursor:
            params["max_id"] = cursor
            params["cursor"] = cursor
        return self._get("/v1/instagram/user/reels", params)

    def iter_reels(self, handle: str, *, max_reels: int | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            page = self.list_reels_page(handle, cursor)
            batch = page.get("items") or []
            if not isinstance(batch, list):
                batch = []
            items.extend(batch)
            log.info("Reels-Seite: +%s (gesamt %s)", len(batch), len(items))
            if max_reels is not None and len(items) >= max_reels:
                return items[:max_reels]
            paging = page.get("paging_info") or {}
            more = bool(
                paging.get("more_available")
                or page.get("has_more")
                or paging.get("has_more")
            )
            nxt = (
                paging.get("max_id")
                or page.get("cursor")
                or paging.get("cursor")
                or page.get("max_id")
            )
            if not more or not nxt or nxt in seen_cursors:
                break
            seen_cursors.add(str(nxt))
            cursor = str(nxt)
        return items

    def get_post(self, permalink: str) -> dict[str, Any]:
        return self._get(
            "/v1/instagram/post",
            {"url": permalink, "cache_max_age": self.cache_max_age},
        )


def parse_handle(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("Handle fehlt.")
    if "instagram.com" in raw:
        path = urlparse(raw).path.strip("/")
        handle = path.split("/")[0]
        return handle.lstrip("@")
    return raw.lstrip("@")


def _dig(obj: Any, *path: str) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _caption_text(media: dict[str, Any]) -> str:
    cap = media.get("caption")
    if isinstance(cap, dict):
        return str(cap.get("text") or "")
    if isinstance(cap, str):
        return cap
    edges = _dig(media, "edge_media_to_caption", "edges")
    if isinstance(edges, list) and edges:
        node = edges[0].get("node") if isinstance(edges[0], dict) else None
        if isinstance(node, dict):
            return str(node.get("text") or "")
    return ""


def _iso_from_taken_at(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str) and "T" in value:
        return value
    try:
        ts = int(value)
        if ts > 10_000_000_000:
            ts //= 1000
        from datetime import datetime, timezone

        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)


def _video_url(media: dict[str, Any]) -> str:
    versions = media.get("video_versions")
    if isinstance(versions, list) and versions:
        first = versions[0]
        if isinstance(first, dict) and first.get("url"):
            return str(first["url"])
    if media.get("video_url"):
        return str(media["video_url"])
    return ""


def unwrap_reel_item(item: dict[str, Any]) -> dict[str, Any]:
    if "media" in item and isinstance(item["media"], dict):
        return item["media"]
    if "data" in item and isinstance(item["data"], dict):
        inner = item["data"]
        if isinstance(inner.get("xdt_shortcode_media"), dict):
            return inner["xdt_shortcode_media"]
        return inner
    if isinstance(item.get("xdt_shortcode_media"), dict):
        return item["xdt_shortcode_media"]
    return item


def extract_music(media: dict[str, Any]) -> dict[str, str]:
    clips = media.get("clips_metadata") or {}
    if not isinstance(clips, dict):
        clips = {}
    music_info = clips.get("music_info") or {}
    asset = {}
    if isinstance(music_info, dict):
        asset = music_info.get("music_asset_info") or music_info
        if not isinstance(asset, dict):
            asset = {}
    original = clips.get("original_sound_info") or {}
    if not isinstance(original, dict):
        original = {}
    attrib = media.get("clips_music_attribution_info") or {}
    if not isinstance(attrib, dict):
        attrib = {}

    ig_title = str(
        asset.get("title")
        or attrib.get("song_name")
        or original.get("original_audio_title")
        or ""
    )
    ig_artist = str(
        asset.get("display_artist")
        or attrib.get("artist_name")
        or _dig(original, "ig_artist", "full_name")
        or _dig(original, "ig_artist", "username")
        or ""
    )
    audio_type = str(clips.get("audio_type") or "")
    if attrib.get("uses_original_audio") and not audio_type:
        audio_type = "original_sounds"
    return {
        "instagram_audio_title": ig_title,
        "instagram_audio_artist": ig_artist,
        "audio_type": audio_type,
        "video_url": _video_url(media),
        "caption": _caption_text(media),
        "shortcode": str(media.get("code") or media.get("shortcode") or ""),
        "permalink": str(
            media.get("url")
            or (
                f"https://www.instagram.com/reel/{media.get('code')}/"
                if media.get("code")
                else ""
            )
        ),
        "taken_at": _iso_from_taken_at(
            media.get("taken_at")
            or media.get("taken_at_timestamp")
            or media.get("created_at")
        ),
    }


def music_is_thin(music: dict[str, str]) -> bool:
    title = (music.get("instagram_audio_title") or "").strip()
    audio_type = music.get("audio_type") or ""
    if not title and not audio_type:
        return True
    from risk_rules import is_original_title

    if is_original_title(title, audio_type) and not music.get("instagram_audio_artist"):
        return False  # Original-Audio ist eine Aussage, kein fehlender Block
    if not title:
        return True
    return False
