from __future__ import annotations

from pathlib import Path

from export_csv import build_summary
from models import ReelRow
from scan_history import is_valid_scan_id, list_scans, load_scan, make_scan_id, save_scan


def test_make_scan_id_is_safe() -> None:
    scan_id = make_scan_id("Krapf Bau!")
    assert is_valid_scan_id(scan_id)
    assert "krapf" in scan_id


def test_save_and_reload_scan(tmp_path: Path) -> None:
    rows = [
        ReelRow(
            permalink="https://www.instagram.com/reel/abc/",
            instagram_audio_title="Love You So",
            instagram_audio_artist="The King Khan & BBQ Show",
            audio_type="licensed_music",
            risk="HIGH",
            handle="krapfbau",
            shortcode="abc",
        )
    ]
    summary = build_summary(rows)
    meta = save_scan(
        tmp_path,
        rows=rows,
        summary=summary,
        handle="krapfbau",
        demo=False,
        mode="metadata",
        force=False,
    )
    assert is_valid_scan_id(meta["id"])
    history = list_scans(tmp_path)
    assert len(history) == 1
    assert history[0]["id"] == meta["id"]
    loaded = load_scan(tmp_path, meta["id"])
    assert loaded is not None
    assert loaded["rows"][0]["instagram_audio_title"] == "Love You So"
    assert (tmp_path / "report.json").exists()
    assert (tmp_path / "scans" / meta["id"] / "report.csv").exists()
