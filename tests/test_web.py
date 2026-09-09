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
    denied = locked.get("/", follow_redirects=False)
    assert denied.status_code == 303
    assert denied.headers["location"].startswith("/login")

    login_page = locked.get("/login")
    assert login_page.status_code == 200
    assert 'name="password"' in login_page.text
    assert "Benutzername" not in login_page.text
    assert 'name="username"' not in login_page.text

    bad = locked.post("/login", data={"password": "falsch"})
    assert bad.status_code == 401
    assert "Falsches Passwort" in bad.text

    ok = locked.post("/login", data={"password": "geheim"}, follow_redirects=False)
    assert ok.status_code == 303
    home = locked.get("/")
    assert home.status_code == 200
    assert "Musik Lizenzen" in home.text
    assert "Abmelden" in home.text


def test_password_only_login_no_username_field(monkeypatch) -> None:
    monkeypatch.setenv("SCANNER_PASSWORD", "geheim")
    client = TestClient(app)
    page = client.get("/login")
    assert page.status_code == 200
    assert 'name="password"' in page.text
    assert 'name="username"' not in page.text
    assert "Benutzername" not in page.text



def test_web_rejects_foreign_handle(client: TestClient) -> None:
    response = client.post(
        "/scan",
        data={"handle": "natgeo", "mode": "metadata"},
    )
    assert response.status_code == 400
    assert "Allowlist" in response.text
    assert "natgeo" in response.text


def test_ui_can_add_and_remove_allowlist_handle(client: TestClient) -> None:
    added = client.post(
        "/allowlist/add",
        data={"handle": "musterbau"},
        follow_redirects=True,
    )
    assert added.status_code == 200
    assert "musterbau" in added.text
    assert "zur Allowlist hinzugefügt" in added.text

    scan_ok = client.post(
        "/scan",
        data={"handle": "musterbau", "demo": "1", "mode": "metadata"},
    )
    assert scan_ok.status_code == 200

    removed = client.post(
        "/allowlist/remove",
        data={"handle": "musterbau"},
        follow_redirects=True,
    )
    assert removed.status_code == 200
    assert "von der Allowlist entfernt" in removed.text

    blocked = client.post(
        "/scan",
        data={"handle": "musterbau", "mode": "metadata"},
    )
    assert blocked.status_code == 400
    assert "Allowlist" in blocked.text


def test_cannot_remove_last_allowlist_handle(client: TestClient) -> None:
    response = client.post(
        "/allowlist/remove",
        data={"handle": "krapfbau"},
    )
    assert response.status_code == 400
    assert "Mindestens ein Handle" in response.text


def test_demo_scan_and_downloads(client: TestClient) -> None:
    response = client.post(
        "/scan",
        data={"handle": "krapfbau", "demo": "1", "mode": "metadata"},
    )
    assert response.status_code == 200
    assert "Love You So" in response.text
    assert "HIGH" in response.text
    assert "Gespeicherte Scans" in response.text
    csv_resp = client.get("/download/csv")
    assert csv_resp.status_code == 200
    assert csv_resp.content.startswith(b"\xef\xbb\xbf")
    json_resp = client.get("/download/json")
    assert json_resp.status_code == 200
    payload = json_resp.json()
    assert payload["summary"]["HIGH"] >= 2
    assert "scan" in payload["summary"]
    scan_id = payload["summary"]["scan"]["id"]
    second = client.post(
        "/scan",
        data={"handle": "krapfbau", "demo": "1", "mode": "metadata"},
    )
    assert second.status_code == 200
    assert "Gespeicherte Scans" in second.text
    history = client.get(f"/?scan={scan_id}")
    assert history.status_code == 200
    assert "Love You So" in history.text
    assert scan_id in history.text
    hist_csv = client.get(f"/download/csv?scan={scan_id}")
    assert hist_csv.status_code == 200
