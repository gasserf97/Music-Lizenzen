from __future__ import annotations

import json
from pathlib import Path

from export_csv import build_summary, write_csv
from models import ReelRow
from risk_rules import classify, is_original_title, matches_watchlist
from scan import apply_risk, demo_rows, parse_args
from scrapecreators_client import extract_music, parse_handle, unwrap_reel_item

FIXTURES = Path(__file__).parent / "fixtures"


def test_love_you_so_is_high() -> None:
    decision = classify(
        ig_title="Love You So",
        ig_artist="The King Khan & BBQ Show",
        audio_type="licensed_music",
    )
    assert decision.risk == "HIGH"
    assert "7.000" in decision.reason or "Love You So" in decision.reason


def test_victory_lap_fingerprint_is_high() -> None:
    decision = classify(
        ig_title="Original audio",
        ig_artist="krapfbau",
        audio_type="original_sounds",
        fingerprint_title="Victory Lap",
        fingerprint_artist="Fred again..",
        fingerprint_label="Atlantic Records",
        fingerprint_match=True,
    )
    assert decision.risk == "HIGH"


def test_unknown_is_not_low() -> None:
    decision = classify(
        ig_title="Original audio",
        ig_artist="krapfbau",
        audio_type="original_sounds",
        fingerprint_match=False,
    )
    assert decision.risk == "UNKNOWN"
    assert "unbedenklich" in decision.reason.lower()


def test_empty_metadata_is_unknown() -> None:
    decision = classify()
    assert decision.risk == "UNKNOWN"


def test_epidemic_is_low() -> None:
    decision = classify(
        ig_title="Night Shift",
        ig_artist="Epidemic Sound",
        audio_type="licensed_music",
    )
    assert decision.risk == "LOW"


def test_licensed_commercial_hit_is_medium() -> None:
    decision = classify(
        ig_title="Espresso",
        ig_artist="Sabrina Carpenter",
        audio_type="licensed_music",
    )
    assert decision.risk == "MEDIUM"
    assert "licensed_music" in decision.reason


def test_official_ig_sound_without_type_is_medium() -> None:
    decision = classify(
        ig_title="Some Indie Track",
        ig_artist="Someone",
        audio_type="",
    )
    assert decision.risk == "MEDIUM"


def test_fingerprint_without_label_is_medium() -> None:
    decision = classify(
        audio_type="original_sounds",
        fingerprint_title="Unknown Club Mix",
        fingerprint_artist="DJ Local",
        fingerprint_match=True,
    )
    assert decision.risk == "MEDIUM"


def test_major_label_hint_is_medium() -> None:
    decision = classify(
        fingerprint_title="Big Song",
        fingerprint_artist="Star",
        fingerprint_label="Warner Music UK",
        fingerprint_match=True,
    )
    assert decision.risk == "MEDIUM"
    assert "Major" in decision.reason or "warner" in decision.reason.lower()


def test_artlist_rights_are_low() -> None:
    decision = classify(
        ig_title="Sunrise Drive",
        ig_artist="Artlist",
        audio_type="licensed_music",
    )
    assert decision.risk == "LOW"
    assert "artlist" in decision.reason.lower()


def test_artlist_label_beats_major_confusion() -> None:
    """Artlist-Rechte bleiben LOW, auch wenn Label-Text verwirrend ist."""
    decision = classify(
        fingerprint_title="Worksite Pulse",
        fingerprint_artist="Studio X",
        fingerprint_label="Artlist / Exclusive",
        fingerprint_match=True,
    )
    assert decision.risk == "LOW"


def test_watchlist_title_only() -> None:
    assert matches_watchlist("Love You So", "")


def test_original_title_detection() -> None:
    assert is_original_title("Original audio", "original_sounds")
    assert is_original_title("Originales Audio")
    assert not is_original_title("Love You So", "licensed_music")


def test_parse_handle() -> None:
    assert parse_handle("https://www.instagram.com/krapfbau/") == "krapfbau"
    assert parse_handle("@krapfbau") == "krapfbau"
    assert parse_handle("krapfbau") == "krapfbau"


def test_extract_music_from_clips_metadata() -> None:
    payload = json.loads((FIXTURES / "scrapecreators_reels.json").read_text(encoding="utf-8"))
    media = unwrap_reel_item(payload["items"][0])
    music = extract_music(media)
    assert music["shortcode"] == "DEiyb48AeB9"
    assert music["audio_type"] == "original_sounds"
    assert music["instagram_audio_title"] == "Original audio"
    assert "instagram.com/reel/DEiyb48AeB9" in music["permalink"]


def test_extract_licensed_sticker() -> None:
    payload = json.loads((FIXTURES / "scrapecreators_reels.json").read_text(encoding="utf-8"))
    media = unwrap_reel_item(payload["items"][1])
    music = extract_music(media)
    assert music["instagram_audio_title"] == "Love You So"
    assert music["instagram_audio_artist"] == "The King Khan & BBQ Show"
    assert music["audio_type"] == "licensed_music"


def test_demo_rows_acceptance() -> None:
    rows = demo_rows()
    risks = {row.shortcode: row.risk for row in rows}
    assert risks["demoLoveYouSo"] == "HIGH"
    assert risks["demoVictoryLap"] == "HIGH"
    assert risks["demoUnknown"] == "UNKNOWN"
    assert risks["demoUnknown"] != "LOW"
    assert risks["demoEpidemic"] == "LOW"
    summary = build_summary(rows)
    assert summary["HIGH"] >= 2
    assert summary["UNKNOWN"] >= 1
    titles = {s["title"] for s in summary["unique_major_label_songs"]}
    assert "Love You So" in titles
    assert "Victory Lap" in titles


def test_csv_excel_header(tmp_path: Path) -> None:
    path = tmp_path / "report.csv"
    write_csv(demo_rows(), path)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = path.read_text(encoding="utf-8-sig")
    assert "permalink" in text.splitlines()[0]
    assert "risk_reason" in text


def test_apply_risk_does_not_invent_fingerprint() -> None:
    row = ReelRow(instagram_audio_title="Original audio", audio_type="original_sounds")
    apply_risk(row)
    assert row.fingerprint_title == ""
    assert row.risk == "UNKNOWN"


def test_cli_requires_input() -> None:
    args = parse_args(["--out", "out/report.csv"])
    assert args.handle is None
    assert args.inbox is None
