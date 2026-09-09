from __future__ import annotations

import json
from pathlib import Path

from export_csv import build_summary
from models import ReelRow
from scan_history import (
    delete_scan,
    is_valid_scan_id,
    list_scans,
    load_scan,
    make_scan_id,
    save_scan,
)


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


def test_delete_scan_removes_files_and_updates_latest(tmp_path: Path) -> None:
    rows = [
        ReelRow(
            permalink="https://www.instagram.com/reel/abc/",
            instagram_audio_title="Love You So",
            risk="HIGH",
            handle="krapfbau",
            shortcode="abc",
        )
    ]
    first = save_scan(
        tmp_path,
        rows=rows,
        summary=build_summary(rows),
        handle="krapfbau",
        demo=False,
        mode="metadata",
        force=False,
    )
    second_rows = [
        ReelRow(
            permalink="https://www.instagram.com/reel/def/",
            instagram_audio_title="Night Shift",
            risk="LOW",
            handle="krapfbau",
            shortcode="def",
        )
    ]
    second = save_scan(
        tmp_path,
        rows=second_rows,
        summary=build_summary(second_rows),
        handle="krapfbau",
        demo=False,
        mode="metadata",
        force=False,
    )
    assert len(list_scans(tmp_path)) == 2
    delete_scan(tmp_path, second["id"])
    assert len(list_scans(tmp_path)) == 1
    assert list_scans(tmp_path)[0]["id"] == first["id"]
    assert not (tmp_path / "scans" / second["id"]).exists()
    latest = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert latest["summary"]["scan"]["id"] == first["id"]
    delete_scan(tmp_path, first["id"])
    assert list_scans(tmp_path) == []
    assert not (tmp_path / "report.json").exists()
