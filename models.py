from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ReelRow:
    permalink: str = ""
    taken_at: str = ""
    caption: str = ""
    instagram_audio_title: str = ""
    instagram_audio_artist: str = ""
    audio_type: str = ""
    fingerprint_title: str = ""
    fingerprint_artist: str = ""
    fingerprint_label: str = ""
    confidence: str = ""
    risk: str = "UNKNOWN"
    risk_reason: str = ""
    action: str = "mit Anwalt prüfen"
    shortcode: str = ""
    handle: str = ""
    source: str = ""
    sha256: str = ""
    skipped: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UsageLog:
    scrapecreators_live: int = 0
    scrapecreators_cached: int = 0
    audd_calls: int = 0
    acrcloud_calls: int = 0
    downloads: int = 0
    skipped_shortcodes: int = 0
    skipped_hashes: int = 0
    errors: list[str] = field(default_factory=list)

    def note_scrape(self, cached: bool) -> None:
        if cached:
            self.scrapecreators_cached += 1
        else:
            self.scrapecreators_live += 1
