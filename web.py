#!/usr/bin/env python3
"""Interne Web-UI für den Reel-Musikrisiko-Scanner (Render / lokal)."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from export_csv import build_summary, write_csv, write_json
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
from scrapecreators_client import parse_handle

load_dotenv(ROOT / ".env")

DEFAULT_HANDLE = "krapfbau"
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))

log = logging.getLogger("scanner.web")
security = HTTPBasic(auto_error=False)

_scan_lock = threading.Lock()
_job: dict[str, Any] = {
    "running": False,
    "error": "",
    "message": "",
}


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


def allowed_handles() -> set[str]:
    return parse_allowed_handles(os.getenv("ALLOWED_HANDLES"))


def handle_is_allowed(handle: str, allowed: set[str] | None = None) -> bool:
    try:
        parsed = parse_handle(handle).lower()
    except ValueError:
        return False
    pool = allowed if allowed is not None else allowed_handles()
    return parsed in {h.lower() for h in pool}


def password_configured() -> bool:
    return bool(os.getenv("SCANNER_PASSWORD", "").strip())


def require_login(
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> None:
    expected = os.getenv("SCANNER_PASSWORD", "").strip()
    if not expected:
        return
    given = (credentials.password if credentials else "") or ""
    if not secrets.compare_digest(given, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Passwort erforderlich",
            headers={"WWW-Authenticate": 'Basic realm="Musik Lizenzen intern"'},
        )


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


def _last_report() -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    path = OUT / "report.json"
    if not path.exists():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None
    if not isinstance(data, dict):
        return [], None
    rows = data.get("rows") or []
    summary = data.get("summary")
    return rows, summary if isinstance(summary, dict) else None


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
    write_csv(rows, OUT / "report.csv")
    summary = build_summary(rows, usage)
    write_json(rows, OUT / "report.json", summary)
    return rows, summary


def _run_job(**kwargs: Any) -> None:
    try:
        execute_scan(**kwargs)
        _job["message"] = "Scan abgeschlossen."
        _job["error"] = ""
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
) -> dict[str, Any]:
    rows, summary = _last_report()
    return {
        "request": request,
        "error": error or (_job.get("error") if not _job.get("running") else "") or "",
        "message": _job.get("message") or "",
        "running": bool(_job.get("running")),
        "password_missing": not password_configured(),
        "allowed": sorted(allowed_handles()),
        "handle": handle,
        "demo": demo,
        "mode": mode or "metadata",
        "force": force,
        "max_reels": max_reels,
        "rows": rows,
        "summary": summary,
        "has_csv": (OUT / "report.csv").exists(),
        "has_json": (OUT / "report.json").exists(),
    }


app = FastAPI(
    title="Musik Lizenzen",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
setup_logging(verbose=False)

if not password_configured():
    log.warning(
        "SCANNER_PASSWORD ist nicht gesetzt. Die UI ist ungeschützt. "
        "Für Render unbedingt setzen."
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _: None = Depends(require_login)) -> HTMLResponse:
    return _page(request)


@app.get("/api/status")
def scan_status(_: None = Depends(require_login)) -> JSONResponse:
    rows, summary = _last_report()
    return JSONResponse(
        {
            "running": bool(_job.get("running")),
            "error": _job.get("error") or "",
            "message": _job.get("message") or "",
            "total": (summary or {}).get("total", 0) if not _job.get("running") else None,
            "has_report": bool(rows),
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
                    f"Dieses Tool scannt nur Konten, die ihr verwaltet ({allow})."
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
            execute_scan(**kwargs)
            _job["error"] = ""
            _job["message"] = (
                "Demo-Scan abgeschlossen (keine Live-APIs, keine Aussage über das echte Konto)."
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
def download_csv(_: None = Depends(require_login)) -> FileResponse:
    path = OUT / "report.csv"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Kein CSV vorhanden. Zuerst scannen.")
    return FileResponse(path, filename="report.csv", media_type="text/csv")


@app.get("/download/json")
def download_json(_: None = Depends(require_login)) -> FileResponse:
    path = OUT / "report.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Kein JSON vorhanden. Zuerst scannen.")
    return FileResponse(path, filename="report.json", media_type="application/json")
