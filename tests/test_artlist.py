from __future__ import annotations

from artlist_client import lookup_artlist, match_catalog
from risk_rules import classify
from scan import apply_risk, demo_rows
from models import ReelRow
from audd_client import FingerprintHit


def test_cadillac_catalog_is_low() -> None:
    hit = match_catalog("My New Cadillac", "Francesco D'Andrea")
    assert hit is not None
    assert hit.source == "catalog"


def test_francesco_artist_catalog_is_artlist() -> None:
    hit = match_catalog("Some Other Cue", "Francesco D'Andrea")
    assert hit is not None
    assert hit.source == "catalog_artist"


def test_classify_artlist_flag_is_low() -> None:
    decision = classify(
        fingerprint_title="My New Cadillac",
        fingerprint_artist="Francesco D'Andrea",
        fingerprint_label="Francesco D'Andrea",
        fingerprint_match=True,
        artlist_licensable=True,
        artlist_note="Artlist-Katalog",
    )
    assert decision.risk == "LOW"
    assert "Artlist" in decision.reason


def test_apply_risk_cadillac_is_low(monkeypatch) -> None:
    monkeypatch.setenv("ARTLIST_LOOKUP", "0")
    row = ReelRow(
        instagram_audio_title="Original audio",
        instagram_audio_artist="krapfbau",
        audio_type="original_sounds",
    )
    fp = FingerprintHit(
        title="My New Cadillac",
        artist="Francesco D'Andrea",
        label="Francesco D'Andrea",
        confidence="match",
    )
    apply_risk(row, fp)
    assert row.risk == "LOW"
    assert "Artlist" in row.risk_reason


def test_lookup_without_network_uses_catalog(monkeypatch) -> None:
    monkeypatch.setenv("ARTLIST_LOOKUP", "0")
    hit = lookup_artlist("My New Cadillac", "Francesco D'Andrea", allow_network=False)
    assert hit is not None
    assert hit.source == "catalog"


def test_demo_includes_artlist_cadillac() -> None:
    rows = demo_rows()
    risks = {row.shortcode: row.risk for row in rows}
    assert risks["demoArtlistCadillac"] == "LOW"
