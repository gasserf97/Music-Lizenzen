from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from export_csv import write_csv, write_json
from models import ReelRow

_HISTORY_LOCK = threading.Lock()
_SAFE_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z_[a-z0-9._-]{1,64}$")


def scans_dir(out: Path) -> Path:
    return out / "scans"


def index_path(out: Path) -> Path:
    return scans_dir(out) / "index.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_scan_id(handle: str, when: datetime | None = None) -> str:
    stamp = (when or _utc_now()).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^a-z0-9._-]+", "-", (handle or "scan").lower()).strip("-") or "scan"
    return f"{stamp}_{safe[:48]}"


def is_valid_scan_id(scan_id: str) -> bool:
    return bool(_SAFE_ID.match(scan_id or ""))


def _read_index(out: Path) -> list[dict[str, Any]]:
    path = index_path(out)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if isinstance(data, dict):
        items = data.get("scans") or []
    elif isinstance(data, list):
        items = data
    else:
        return []
    return [item for item in items if isinstance(item, dict) and item.get("id")]


def _write_index(out: Path, entries: list[dict[str, Any]]) -> None:
    path = index_path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"scans": entries}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def list_scans(out: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    with _HISTORY_LOCK:
        entries = _read_index(out)
    entries.sort(key=lambda item: str(item.get("created_at") or item.get("id") or ""), reverse=True)
    return entries[:limit]


def load_scan(out: Path, scan_id: str) -> dict[str, Any] | None:
    if not is_valid_scan_id(scan_id):
        return None
    path = scans_dir(out) / scan_id / "report.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("id", scan_id)
    return data


def scan_paths(out: Path, scan_id: str) -> tuple[Path, Path]:
    folder = scans_dir(out) / scan_id
    return folder / "report.csv", folder / "report.json"


def save_scan(
    out: Path,
    *,
    rows: list[ReelRow],
    summary: dict[str, Any],
    handle: str,
    demo: bool,
    mode: str,
    force: bool,
) -> dict[str, Any]:
    """Persistiert einen Scan und aktualisiert index + latest report.*."""
    created = _utc_now()
    label = "demo" if demo else (handle or "scan")
    scan_id = make_scan_id(label, created)
    folder = scans_dir(out) / scan_id
    folder.mkdir(parents=True, exist_ok=True)

    meta = {
        "id": scan_id,
        "created_at": created.isoformat(),
        "handle": "demo" if demo else handle,
        "demo": demo,
        "mode": mode,
        "force": force,
        "total": summary.get("total", len(rows)),
        "HIGH": summary.get("HIGH", 0),
        "MEDIUM": summary.get("MEDIUM", 0),
        "LOW": summary.get("LOW", 0),
        "UNKNOWN": summary.get("UNKNOWN", 0),
    }
    enriched_summary = {**summary, "scan": meta}
    csv_path, json_path = scan_paths(out, scan_id)
    write_csv(rows, csv_path)
    write_json(rows, json_path, enriched_summary)

    # latest shortcuts for existing download links
    write_csv(rows, out / "report.csv")
    write_json(rows, out / "report.json", enriched_summary)

    with _HISTORY_LOCK:
        entries = [item for item in _read_index(out) if item.get("id") != scan_id]
        entries.insert(0, meta)
        _write_index(out, entries[:200])

    return meta


def ensure_history_from_latest(out: Path) -> None:
    """Übernimmt vorhandenes report.json einmalig in die Historie."""
    latest = out / "report.json"
    if not latest.exists():
        return
    with _HISTORY_LOCK:
        if _read_index(out):
            return
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(data, dict) or not data.get("rows"):
        return
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    scan_meta = summary.get("scan") if isinstance(summary.get("scan"), dict) else {}
    if scan_meta.get("id") and is_valid_scan_id(str(scan_meta["id"])):
        # already structured
        with _HISTORY_LOCK:
            if not _read_index(out):
                _write_index(out, [scan_meta])
        return
    rows_raw = data.get("rows") or []
    rows = [ReelRow(**{k: row.get(k, "") for k in ReelRow().__dict__}) for row in rows_raw if isinstance(row, dict)]
    if not rows:
        return
    handle = ""
    for row in rows:
        if row.handle:
            handle = row.handle
            break
    save_scan(
        out,
        rows=rows,
        summary=summary or {"total": len(rows)},
        handle=handle or "import",
        demo=False,
        mode="unknown",
        force=False,
    )
