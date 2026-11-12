"""Mock of the PC QR gate API (contract in api-contract/, not in Git).

Canned responses only, no business logic. Token in the scanned value picks
the validate outcome: contains "reject" -> rejected, contains "pay" (exit)
-> payment_required, anything else -> allowed.

Run: MOCK_API_KEY=dev-key uvicorn mock_server:app --host 0.0.0.0 --port 8000
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

API_KEY = os.environ.get("MOCK_API_KEY", "dev-key")
ESTATE_CODE = os.environ.get("MOCK_ESTATE_CODE", "DEMO")


def now(offset_min: int = 0) -> str:
    t = datetime.now(timezone.utc) + timedelta(minutes=offset_min)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def check_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(401, {"code": "INVALID_API_KEY",
                                  "error": "Akses API ditolak."})


app = FastAPI(title="PC QR API mock", dependencies=[Depends(check_key)])


@app.exception_handler(HTTPException)
async def flat_error(_req, exc: HTTPException):
    return JSONResponse(exc.detail, status_code=exc.status_code)


@app.get("/api/pc-qr/{estate_id}/entries")
def entries(estate_id: int, include_upcoming: int = 0):
    entry = {"qr_token": "contoh-token-qr", "valid_until": now(60)}
    if include_upcoming:
        entry["valid_from"] = now(-30)
    return {
        "schema": "ipop-gate-qr-list/v1",
        "estate_code": ESTATE_CODE,
        "generated_at": now(),
        "cache_valid_until": now(360 if include_upcoming else 15),
        "entries": [entry],
    }


@app.post("/api/pc-qr/{estate_id}/validate")
def validate(estate_id: int, body: dict):
    token = str(body.get("value", "")).rstrip("/").rsplit("/", 1)[-1]
    direction = body.get("direction", "entry")
    if direction not in ("entry", "exit"):
        raise HTTPException(400, {"code": "INVALID_DIRECTION",
                                  "error": "direction harus entry atau exit."})
    if not token:
        return {"decision": "rejected", "reason": "qr_tidak_valid"}
    if "reject" in token:
        return {"decision": "rejected", "reason": "qr_tidak_terdaftar"}
    if "pay" in token and direction == "exit":
        return {"decision": "payment_required", "reason": "tarif_parkir",
                "delivery_id": 123, "tracking_code": "DEMO-20261111-001",
                "fee_amount": 6500, "payment_status": "pending"}
    return {
        "decision": "allowed",
        "reason": "boleh_keluar" if direction == "exit" else "sesuai_jadwal",
        "direction": direction,
        "event_id": str(uuid.uuid4()),
        "authorized_at": now(),
        "delivery_id": 123,
        "tracking_code": "DEMO-20261111-001",
        "fee_amount": 0,
    }


@app.post("/api/pc-qr/{estate_id}/events")
def events(estate_id: int, body: dict):
    if not body.get("event_id") or not body.get("qr_token"):
        raise HTTPException(400, {"code": "EVENT_ERROR",
                                  "error": "Laporan gate tidak valid."})
    return {"ok": True, "accepted": True, "duplicate": False, "reason": None}


@app.post("/api/pc-qr/{estate_id}/events/batch")
def events_batch(estate_id: int, body: dict):
    evs = body.get("events")
    if not isinstance(evs, list) or len(evs) > 200:
        raise HTTPException(400, {"code": "INVALID_BATCH",
                                  "error": "Batch laporan tidak valid."})
    return {"ok": True, "results": [
        {"event_id": e.get("event_id"), "ok": True, "accepted": True,
         "duplicate": False, "reason": None} for e in evs]}


@app.post("/api/pc-qr/{estate_id}/cancel")
def cancel(estate_id: int, body: dict):
    if not body.get("event_id"):
        raise HTTPException(400, {"code": "CANCEL_ERROR",
                                  "error": "event_id wajib diisi."})
    return {"ok": True, "released": True}


@app.post("/api/pc-qr/{estate_id}/heartbeat")
def heartbeat(estate_id: int, body: dict):
    skew = 0
    if body.get("pc_time"):
        pc = datetime.fromisoformat(body["pc_time"].replace("Z", "+00:00"))
        skew = int((pc - datetime.now(timezone.utc)).total_seconds() * 1000)
    return {"ok": True, "server_time": now(), "clock_skew_ms": skew,
            "clock_ok": abs(skew) <= 60_000}


@app.post("/api/pc-qr/{estate_id}/card-events")
def card_events(estate_id: int, body: dict):
    outcome = None
    if body.get("qr_token"):
        outcome = "exited" if body.get("direction") == "exit" else "entered"
    return {"ok": True, "duplicate": False,
            "delivery_id": 123 if outcome else None,
            "outcome": outcome, "outstanding_fee": 0}


@app.post("/api/pc-qr/{estate_id}/vehicle-capture", status_code=201)
async def vehicle_capture(estate_id: int, request: Request,
                          x_qr_token: str = Header(...),
                          x_gate_direction: str = Header("entry")):
    data = await request.body()
    if len(data) > 3 * 1024 * 1024:
        raise HTTPException(413, {"code": "PAYLOAD_TOO_LARGE",
                                  "error": "Ukuran data melebihi batas."})
    if not data.startswith(b"\xff\xd8"):
        raise HTTPException(400, {"code": "INVALID_IMAGE",
                                  "error": "File harus JPEG."})
    return {"ok": True, "delivery_id": 123, "direction": x_gate_direction}
