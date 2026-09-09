from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("scanner.audd")


@dataclass
class FingerprintHit:
    title: str = ""
    artist: str = ""
    label: str = ""
    confidence: str = ""
    provider: str = ""
    raw: dict[str, Any] | None = None

    @property
    def matched(self) -> bool:
        return bool(self.title or self.artist)


class AuddError(RuntimeError):
    pass


def _label_from_audd(result: dict[str, Any]) -> str:
    if result.get("label"):
        return str(result["label"])
    apple = result.get("apple_music") or {}
    if isinstance(apple, dict):
        for key in ("recordLabel", "record_label", "label"):
            if apple.get(key):
                return str(apple[key])
        recs = apple.get("recordLabels") or apple.get("record_labels") or []
        if isinstance(recs, list) and recs:
            first = recs[0]
            if isinstance(first, dict) and first.get("name"):
                return str(first["name"])
            if isinstance(first, str):
                return first
    return ""


class AuddClient:
    def __init__(
        self,
        token: str,
        *,
        timeout: int = 90,
        usage: Any | None = None,
        enterprise: bool = False,
    ) -> None:
        if not token:
            raise AuddError("AUDD_TOKEN fehlt.")
        self.token = token
        self.timeout = timeout
        self.usage = usage
        self.enterprise = enterprise

    def recognize_file(self, path: Path) -> FingerprintHit:
        endpoint = "https://enterprise.audd.io/" if self.enterprise else "https://api.audd.io/"
        data = {
            "api_token": self.token,
            "return": "apple_music,spotify",
        }
        if self.enterprise:
            data["accurate_offsets"] = "true"
        log.info("AudD recognize %s (%s)", path.name, endpoint)
        if self.usage is not None:
            self.usage.audd_calls += 1
        with path.open("rb") as handle:
            resp = requests.post(
                endpoint,
                data=data,
                files={"file": (path.name, handle, "audio/mpeg")},
                timeout=self.timeout,
            )
        if resp.status_code >= 400:
            raise AuddError(f"AudD HTTP {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        if payload.get("status") == "error":
            err = payload.get("error") or payload
            raise AuddError(f"AudD Fehler: {err}")
        result = payload.get("result")
        if not result:
            log.info("AudD: kein Treffer für %s", path.name)
            return FingerprintHit(provider="audd", raw=payload)
        if isinstance(result, list):
            # Enterprise kann eine Trackliste liefern
            first = result[0] if result else {}
            if isinstance(first, dict) and "songs" in first:
                songs = first.get("songs") or []
                first = songs[0] if songs else {}
            result = first
        if not isinstance(result, dict):
            return FingerprintHit(provider="audd", raw=payload)
        score = result.get("score") or result.get("confidence") or "match"
        return FingerprintHit(
            title=str(result.get("title") or ""),
            artist=str(result.get("artist") or ""),
            label=_label_from_audd(result),
            confidence=str(score),
            provider="audd",
            raw=payload,
        )


class AcrCloudClient:
    def __init__(
        self,
        host: str,
        access_key: str,
        access_secret: str,
        *,
        timeout: int = 60,
        usage: Any | None = None,
    ) -> None:
        self.host = host
        self.access_key = access_key
        self.access_secret = access_secret
        self.timeout = timeout
        self.usage = usage

    def recognize_file(self, path: Path) -> FingerprintHit:
        http_uri = "/v1/identify"
        data_type = "audio"
        signature_version = "1"
        timestamp = str(int(time.time()))
        string_to_sign = "\n".join(
            ["POST", http_uri, self.access_key, data_type, signature_version, timestamp]
        )
        sign = base64.b64encode(
            hmac.new(
                self.access_secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("utf-8")
        sample = path.read_bytes()
        files = {"sample": (path.name, sample, "audio/mpeg")}
        data = {
            "access_key": self.access_key,
            "sample_bytes": str(len(sample)),
            "timestamp": timestamp,
            "signature": sign,
            "data_type": data_type,
            "signature_version": signature_version,
        }
        url = f"https://{self.host}{http_uri}"
        log.info("ACRCloud recognize %s", path.name)
        if self.usage is not None:
            self.usage.acrcloud_calls += 1
        resp = requests.post(url, files=files, data=data, timeout=self.timeout)
        if resp.status_code >= 400:
            raise AuddError(f"ACRCloud HTTP {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        music = (((payload.get("metadata") or {}).get("music")) or [])
        if not music:
            return FingerprintHit(provider="acrcloud", raw=payload)
        first = music[0]
        artists = first.get("artists") or []
        artist = ""
        if artists and isinstance(artists[0], dict):
            artist = str(artists[0].get("name") or "")
        label = ""
        label_obj = first.get("label") or first.get("label_name")
        if isinstance(label_obj, dict):
            label = str(label_obj.get("name") or "")
        elif label_obj:
            label = str(label_obj)
        score = first.get("score") or first.get("play_offset_ms") or "match"
        return FingerprintHit(
            title=str(first.get("title") or ""),
            artist=artist,
            label=label,
            confidence=str(score),
            provider="acrcloud",
            raw=payload,
        )
