from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web import app, handle_is_allowed, parse_allowed_handles, parse_scan_mode


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("SCANNER_PASSWORD", raising=False)
    monkeypatch.setenv("ALLOWED_HANDLES", "krapfbau")
    monkeypatch.delenv("SCRAPECREATORS_API_KEY", raising=False)
    monkeypatch.setattr("web.OUT", tmp_path)
    monkeypatch.setattr("scan.STATE_PATH", tmp_path / "state.json")
    return TestClient(app)


def test_parse_allowed_handles_default() -> None:
    assert parse_allowed_handles(None) == {"krapfbau"}
    assert parse_allowed_handles("") == {"krapfbau"}
    assert parse_allowed_handles("krapfbau, AndererMandant") == {"krapfbau", "anderermandant"}


def test_handle_allowlist_rejects_third_parties() -> None:
    allowed = {"krapfbau"}
    assert handle_is_allowed("krapfbau", allowed)
    assert handle_is_allowed("@krapfbau", allowed)
    assert handle_is_allowed("https://www.instagram.com/krapfbau/", allowed)
    assert not handle_is_allowed("natgeo", allowed)
    assert not handle_is_allowed("selenagomez", allowed)
    assert not handle_is_allowed("", allowed)


def test_parse_scan_mode_defaults_to_metadata() -> None:
    assert parse_scan_mode("") == (True, False)
    assert parse_scan_mode("metadata") == (True, False)
    assert parse_scan_mode("cheap") == (False, True)
    assert parse_scan_mode("full") == (False, False)


def test_health_open_without_password(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_open_even_with_password(monkeypatch) -> None:
    monkeypatch.setenv("SCANNER_PASSWORD", "geheim")
    response = TestClient(app).get("/health")
    assert response.status_code == 200


def test_ui_requires_password(monkeypatch) -> None:
    monkeypatch.setenv("SCANNER_PASSWORD", "geheim")
    locked = TestClient(app)
    denied = locked.get("/")
    assert denied.status_code == 401
    ok = locked.get("/", auth=("intern", "geheim"))
    assert ok.status_code == 200
    assert "Musik Lizenzen" in ok.text


def test_web_rejects_foreign_handle(client: TestClient) -> None:
    response = client.post(
        "/scan",
        data={"handle": "natgeo", "mode": "metadata"},
    )
    assert response.status_code == 400
    assert "Allowlist" in response.text
    assert "natgeo" in response.text


def test_demo_scan_and_downloads(client: TestClient) -> None:
    response = client.post(
        "/scan",
        data={"handle": "krapfbau", "demo": "1", "mode": "metadata"},
    )
    assert response.status_code == 200
    assert "Love You So" in response.text
    assert "HIGH" in response.text
    csv_resp = client.get("/download/csv")
    assert csv_resp.status_code == 200
    assert csv_resp.content.startswith(b"\xef\xbb\xbf")
    json_resp = client.get("/download/json")
    assert json_resp.status_code == 200
    payload = json_resp.json()
    assert payload["summary"]["HIGH"] >= 2
