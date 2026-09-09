#!/usr/bin/env python3
"""Interne Web-UI für den Reel-Musikrisiko-Scanner (Render / lokal)."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from export_csv import build_summary
from models import ReelRow, UsageLog
from scan import (
    OUT,
    ROOT,
    build_clients,
    demo_rows,
    load_state,
    merge_rows,
    save_state,
    scan_handle,
    setup_logging,
)
from scan_history import (
    ensure_history_from_latest,
    is_valid_scan_id,
    list_scans,
    load_scan,
    save_scan,
    scan_paths,
)
from scrapecreators_client import parse_handle

load_dotenv(ROOT / ".env")

DEFAULT_HANDLE = "krapfbau"
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))
SESSION_AUTH_KEY = "authenticated"

log = logging.getLogger("scanner.web")

_scan_lock = threading.Lock()
_allowlist_lock = threading.Lock()
_job: dict[str, Any] = {
    "running": False,
    "error": "",
    "message": "",
    "last_scan_id": "",
}


class LoginRequired(Exception):
    """Nicht angemeldet — Redirect zur Passwort-Seite."""


def allowlist_path() -> Path:
    return OUT / "allowed_handles.json"


def parse_allowed_handles(raw: str | None = None) -> set[str]:
    """Allowlist aus Komma-Liste. Leer/None → nur krapfbau."""
    text = DEFAULT_HANDLE if raw is None else raw
    found: set[str] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            found.add(parse_handle(part).lower())
        except ValueError:
            continue
    return found or {DEFAULT_HANDLE}


def _seed_handles_from_env() -> set[str]:
    return parse_allowed_handles(os.getenv("ALLOWED_HANDLES"))


def _read_stored_handles() -> set[str] | None:
    path = allowlist_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    raw_items: list[Any]
    if isinstance(data, dict):
        raw_items = list(data.get("handles") or [])
    elif isinstance(data, list):
        raw_items = data
    else:
        return None
    found: set[str] = set()
    for item in raw_items:
        try:
            found.add(parse_handle(str(item)).lower())
        except ValueError:
            continue
    return found


def _write_stored_handles(handles: set[str]) -> None:
    path = allowlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"handles": sorted(handles)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def allowed_handles() -> set[str]:
    """Effektive Allowlist: UI-Datei, sonst Seed aus ALLOWED_HANDLES."""
    with _allowlist_lock:
        stored = _read_stored_handles()
        if stored:
            return set(stored)
        seed = _seed_handles_from_env()
        _write_stored_handles(seed)
        return set(seed)


def add_allowed_handle(handle: str) -> str:
    parsed = parse_handle(handle).lower()
    with _allowlist_lock:
        stored = _read_stored_handles()
        current = set(stored) if stored else _seed_handles_from_env()
        current.add(parsed)
        _write_stored_handles(current)
    return parsed


def remove_allowed_handle(handle: str) -> str:
    parsed = parse_handle(handle).lower()
    with _allowlist_lock:
        stored = _read_stored_handles()
        current = set(stored) if stored else _seed_handles_from_env()
        if parsed not in current:
            raise ValueError(f"@{parsed} steht nicht auf der Allowlist.")
        if len(current) <= 1:
            raise ValueError("Mindestens ein Handle muss auf der Allowlist bleiben.")
        current.discard(parsed)
        _write_stored_handles(current)
    return parsed


def handle_is_allowed(handle: str, allowed: set[str] | None = None) -> bool:
    try:
        parsed = parse_handle(handle).lower()
    except ValueError:
        return False
    pool = allowed if allowed is not None else allowed_handles()
    return parsed in {h.lower() for h in pool}


def password_configured() -> bool:
    return bool(os.getenv("SCANNER_PASSWORD", "").strip())


def session_secret() -> str:
    """Cookie-Signatur. Prefer SESSION_SECRET, sonst SCANNER_PASSWORD, sonst Dev-Fallback."""
    return (
        os.getenv("SESSION_SECRET", "").strip()
        or os.getenv("SCANNER_PASSWORD", "").strip()
        or "dev-insecure-session-secret"
    )


def is_authenticated(request: Request) -> bool:
    if not password_configured():
        return True
    return bool(request.session.get(SESSION_AUTH_KEY))


def require_login(request: Request) -> None:
    if is_authenticated(request):
        return
    raise LoginRequired()


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "on", "true", "yes", "ja"}


def parse_scan_mode(mode: str) -> tuple[bool, bool]:
    """Gibt (metadata_only, cheap) zurück. Standard: nur Metadaten."""
    value = (mode or "metadata").strip().lower()
    if value == "cheap":
        return False, True
    if value == "full":
        return False, False
    return True, False


def _last_report(
    scan_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None]:
    ensure_history_from_latest(OUT)
    if scan_id:
        data = load_scan(OUT, scan_id)
        if not data:
            return [], None, None
        rows = data.get("rows") or []
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else None
        meta = None
        if summary and isinstance(summary.get("scan"), dict):
            meta = summary["scan"]
        else:
            meta = {"id": scan_id}
        return rows, summary, meta

    path = OUT / "report.json"
    if not path.exists():
        return [], None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None, None
    if not isinstance(data, dict):
        return [], None, None
    rows = data.get("rows") or []
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else None
    meta = None
    if summary and isinstance(summary.get("scan"), dict):
        meta = summary["scan"]
    return rows, summary, meta


def execute_scan(
    *,
    handle: str,
    demo: bool,
    metadata_only: bool,
    cheap: bool,
    force: bool,
    max_reels: int | None,
) -> tuple[list[ReelRow], dict[str, Any]]:
    usage = UsageLog()
    state = load_state() if not force else {"shortcodes": {}, "hashes": {}}
    groups: list[list[ReelRow]] = []

    if demo:
        log.info("Demo-Modus über Web-UI — keine Live-APIs.")
        groups.append(demo_rows())
    else:
        parsed = parse_handle(handle)
        if not handle_is_allowed(parsed):
            raise PermissionError(
                f"Handle @{parsed} ist nicht erlaubt. "
                "Nur verwaltete Konten (ALLOWED_HANDLES). "
                "Kein Scan fremder Instagram-Profile."
            )
        args = SimpleNamespace(sleep=0.5, cache_max_age="7d", enterprise=False)
        scrape, audd, acr = build_clients(usage, args)
        if scrape is None:
            raise RuntimeError(
                "SCRAPECREATORS_API_KEY fehlt. Live-Scan nicht möglich. "
                "Demo-Scan nutzen oder den Key auf Render setzen."
            )
        if not metadata_only and audd is None and acr is None:
            log.warning("AUDD_TOKEN fehlt — Fingerprint wird übersprungen.")
        log.info("Web-Scan @%s metadata_only=%s cheap=%s", parsed, metadata_only, cheap)
        groups.append(
            scan_handle(
                parsed,
                client=scrape,
                audd=audd,
                acr=acr,
                usage=usage,
                state=state,
                force=force,
                cheap=cheap,
                metadata_only=metadata_only,
                max_reels=max_reels,
                always_fingerprint=False,
            )
        )

    rows = merge_rows(*groups)
    save_state(state)
    summary = build_summary(rows, usage)
    mode = "demo" if demo else ("cheap" if cheap else ("metadata" if metadata_only else "full"))
    meta = save_scan(
        OUT,
        rows=rows,
        summary=summary,
        handle=handle,
        demo=demo,
        mode=mode,
        force=force,
    )
    summary = {**summary, "scan": meta}
    return rows, summary


def _run_job(**kwargs: Any) -> None:
    try:
        _rows, summary = execute_scan(**kwargs)
        scan_meta = (summary or {}).get("scan") or {}
        scan_id = scan_meta.get("id") or ""
        _job["message"] = (
            f"Scan abgeschlossen und gespeichert ({scan_id})."
            if scan_id
            else "Scan abgeschlossen und gespeichert."
        )
        _job["error"] = ""
        _job["last_scan_id"] = scan_id
    except Exception as exc:
        log.exception("Web-Scan fehlgeschlagen")
        _job["error"] = str(exc)
        _job["message"] = ""
    finally:
        _job["running"] = False


def _page(
    request: Request,
    *,
    status_code: int = 200,
    **kwargs: Any,
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        _form_context(request, **kwargs),
        status_code=status_code,
    )


def _form_context(
    request: Request,
    *,
    error: str = "",
    handle: str = DEFAULT_HANDLE,
    demo: bool = False,
    mode: str = "metadata",
    force: bool = False,
    max_reels: str = "",
    scan_id: str | None = None,
) -> dict[str, Any]:
    rows, summary, active_scan = _last_report(scan_id)
    history = list_scans(OUT)
    active_id = (active_scan or {}).get("id") or scan_id or ""
    return {
        "request": request,
        "error": error or (_job.get("error") if not _job.get("running") else "") or "",
        "message": _job.get("message") or "",
        "running": bool(_job.get("running")),
        "password_missing": not password_configured(),
        "password_configured": password_configured(),
        "allowed": sorted(allowed_handles()),
        "handle": handle,
        "demo": demo,
        "mode": mode or "metadata",
        "force": force,
        "max_reels": max_reels,
        "rows": rows,
        "summary": summary,
        "active_scan": active_scan,
        "history": history,
        "active_scan_id": active_id,
        "has_csv": bool(active_id and scan_paths(OUT, str(active_id))[0].exists())
        or (OUT / "report.csv").exists(),
        "has_json": bool(active_id and scan_paths(OUT, str(active_id))[1].exists())
        or (OUT / "report.json").exists(),
    }


app = FastAPI(
    title="Musik Lizenzen",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(),
    same_site="lax",
    https_only=os.getenv("RENDER", "").lower() == "true"
    or os.getenv("FORCE_HTTPS_COOKIES", "").lower() in {"1", "true", "yes"},
    max_age=60 * 60 * 12,
)
setup_logging(verbose=False)

if not password_configured():
    log.warning(
        "SCANNER_PASSWORD ist nicht gesetzt. Die UI ist ungeschützt. "
        "Für Render unbedingt setzen."
    )


@app.exception_handler(LoginRequired)
async def login_required_handler(request: Request, _exc: LoginRequired) -> RedirectResponse:
    next_path = request.url.path
    if next_path in {"/login", "/logout"}:
        next_path = "/"
    target = "/login"
    if next_path and next_path != "/":
        target = f"/login?next={next_path}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/") -> HTMLResponse:
    if is_authenticated(request):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    dest = next if next.startswith("/") and not next.startswith("//") else "/"
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {"request": request, "error": "", "next": dest},
    )


@app.post("/login", response_model=None)
def login_submit(
    request: Request,
    password: str = Form(""),
    next: str = Form("/"),
) -> RedirectResponse | HTMLResponse:
    expected = os.getenv("SCANNER_PASSWORD", "").strip()
    dest = next if next.startswith("/") and not next.startswith("//") else "/"
    if not expected:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    given = password or ""
    if not secrets.compare_digest(given, expected):
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": "Falsches Passwort.", "next": dest},
            status_code=401,
        )
    request.session[SESSION_AUTH_KEY] = True
    return RedirectResponse(dest, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    _: None = Depends(require_login),
    scan: str = "",
) -> HTMLResponse:
    scan_id = scan.strip() or None
    if scan_id and not is_valid_scan_id(scan_id):
        return _page(request, error="Ungültige Scan-ID.", status_code=400)
    if scan_id and load_scan(OUT, scan_id) is None:
        return _page(request, error="Gespeicherter Scan nicht gefunden.", status_code=404)
    return _page(request, scan_id=scan_id)


@app.post("/allowlist/add", response_model=None)
def allowlist_add(
    request: Request,
    _: None = Depends(require_login),
    handle: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    raw = (handle or "").strip()
    try:
        added = add_allowed_handle(raw)
    except ValueError as exc:
        return _page(request, error=str(exc), handle=raw or DEFAULT_HANDLE, status_code=400)
    _job["error"] = ""
    _job["message"] = f"@{added} zur Allowlist hinzugefügt."
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/allowlist/remove", response_model=None)
def allowlist_remove(
    request: Request,
    _: None = Depends(require_login),
    handle: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    raw = (handle or "").strip()
    try:
        removed = remove_allowed_handle(raw)
    except ValueError as exc:
        return _page(request, error=str(exc), handle=raw or DEFAULT_HANDLE, status_code=400)
    _job["error"] = ""
    _job["message"] = f"@{removed} von der Allowlist entfernt."
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/api/status")
def scan_status(_: None = Depends(require_login)) -> JSONResponse:
    rows, summary, active = _last_report()
    return JSONResponse(
        {
            "running": bool(_job.get("running")),
            "error": _job.get("error") or "",
            "message": _job.get("message") or "",
            "total": (summary or {}).get("total", 0) if not _job.get("running") else None,
            "has_report": bool(rows),
            "last_scan_id": (active or {}).get("id") or _job.get("last_scan_id") or "",
        }
    )


def _error_page(
    request: Request,
    error: str,
    *,
    handle: str,
    demo: bool,
    mode: str,
    force: bool,
    max_reels: str,
    status_code: int,
) -> HTMLResponse:
    return _page(
        request,
        status_code=status_code,
        error=error,
        handle=handle,
        demo=demo,
        mode=mode,
        force=force,
        max_reels=max_reels,
    )


@app.post("/scan", response_model=None)
def start_scan(
    request: Request,
    _: None = Depends(require_login),
    handle: str = Form(DEFAULT_HANDLE),
    demo: str = Form(""),
    mode: str = Form("metadata"),
    force: str = Form(""),
    max_reels: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    is_demo = _truthy(demo)
    is_force = _truthy(force)
    mode_value = (mode or "metadata").strip().lower()
    if mode_value not in {"metadata", "cheap", "full"}:
        mode_value = "metadata"
    metadata_only, cheap = parse_scan_mode(mode_value)
    raw_handle = (handle or DEFAULT_HANDLE).strip() or DEFAULT_HANDLE
    raw_limit = (max_reels or "").strip()

    limit: int | None = None
    if raw_limit:
        try:
            limit = int(raw_limit)
            if limit < 1:
                raise ValueError
        except ValueError:
            return _error_page(
                request,
                "max_reels muss eine positive Zahl sein.",
                handle=raw_handle,
                demo=is_demo,
                mode=mode_value,
                force=is_force,
                max_reels=raw_limit,
                status_code=400,
            )

    if not is_demo:
        try:
            parsed = parse_handle(raw_handle)
        except ValueError:
            return _error_page(
                request,
                "Handle fehlt oder ist ungültig.",
                handle=raw_handle,
                demo=is_demo,
                mode=mode_value,
                force=is_force,
                max_reels=raw_limit,
                status_code=400,
            )
        if not handle_is_allowed(parsed):
            allow = ", ".join("@" + h for h in sorted(allowed_handles()))
            return _error_page(
                request,
                (
                    f"@{parsed} steht nicht auf der Allowlist. "
                    f"Unten unter „Kunden / Allowlist“ hinzufügen. "
                    f"Aktuell erlaubt: {allow}."
                ),
                handle=raw_handle,
                demo=is_demo,
                mode=mode_value,
                force=is_force,
                max_reels=raw_limit,
                status_code=400,
            )
        if not os.getenv("SCRAPECREATORS_API_KEY", "").strip():
            return _error_page(
                request,
                (
                    "SCRAPECREATORS_API_KEY fehlt. Live-Scan nicht möglich. "
                    "Demo-Scan nutzen oder den Key in den Render-Umgebungsvariablen setzen."
                ),
                handle=raw_handle,
                demo=is_demo,
                mode=mode_value,
                force=is_force,
                max_reels=raw_limit,
                status_code=400,
            )

    kwargs = {
        "handle": raw_handle,
        "demo": is_demo,
        "metadata_only": metadata_only,
        "cheap": cheap,
        "force": is_force,
        "max_reels": limit,
    }

    if is_demo:
        try:
            _rows, summary = execute_scan(**kwargs)
            scan_meta = (summary or {}).get("scan") or {}
            scan_id = scan_meta.get("id") or ""
            _job["error"] = ""
            _job["last_scan_id"] = scan_id
            _job["message"] = (
                "Demo-Scan abgeschlossen und gespeichert"
                + (f" ({scan_id})" if scan_id else "")
                + " — keine Live-APIs, keine Aussage über das echte Konto."
            )
        except Exception as exc:
            return _error_page(
                request,
                str(exc),
                handle=raw_handle,
                demo=True,
                mode=mode_value,
                force=is_force,
                max_reels=raw_limit,
                status_code=500,
            )
        return _page(
            request,
            handle=raw_handle,
            demo=True,
            mode=mode_value,
            force=is_force,
            max_reels=raw_limit,
            scan_id=scan_id or None,
        )

    with _scan_lock:
        if _job["running"]:
            return _error_page(
                request,
                "Es läuft bereits ein Scan. Bitte warten.",
                handle=raw_handle,
                demo=False,
                mode=mode_value,
                force=is_force,
                max_reels=raw_limit,
                status_code=409,
            )
        _job["running"] = True
        _job["error"] = ""
        _job["message"] = "Scan läuft …"
        threading.Thread(target=_run_job, kwargs=kwargs, daemon=True).start()

    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/download/csv")
def download_csv(
    _: None = Depends(require_login),
    scan: str = "",
) -> FileResponse:
    scan_id = scan.strip()
    if scan_id:
        if not is_valid_scan_id(scan_id):
            raise HTTPException(status_code=400, detail="Ungültige Scan-ID.")
        path, _json = scan_paths(OUT, scan_id)
        filename = f"report_{scan_id}.csv"
    else:
        path = OUT / "report.csv"
        filename = "report.csv"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Kein CSV vorhanden. Zuerst scannen.")
    return FileResponse(path, filename=filename, media_type="text/csv")


@app.get("/download/json")
def download_json(
    _: None = Depends(require_login),
    scan: str = "",
) -> FileResponse:
    scan_id = scan.strip()
    if scan_id:
        if not is_valid_scan_id(scan_id):
            raise HTTPException(status_code=400, detail="Ungültige Scan-ID.")
        _csv, path = scan_paths(OUT, scan_id)
        filename = f"report_{scan_id}.json"
    else:
        path = OUT / "report.json"
        filename = "report.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Kein JSON vorhanden. Zuerst scannen.")
    return FileResponse(path, filename=filename, media_type="application/json")
