#!/usr/bin/env python3
"""Send test requests to the PC QR API (real server or mock_server.py).

Stdlib only, so it runs on the Pi without pip. Settings and the API key
live in config.json next to this file (copy config.example.json).

Examples:
  python3 gate_client.py entries --upcoming
  python3 gate_client.py validate https://gate.example.com/verify/abc
  python3 gate_client.py scan abc --exit       # validate, then report open
  python3 gate_client.py heartbeat
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).with_name("config.json")


def now() -> str:
    t = datetime.now(timezone.utc)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Client:
    def __init__(self, cfg: dict):
        base_url = cfg["base_url"].rstrip("/")
        self.base = f"{base_url}/api/pc-qr/{cfg['estate_id']}"
        self.key = cfg["api_key"]
        self.estate_code = cfg.get("estate_code", "DEMO")
        self.timeout = cfg.get("timeout_s", 5)

    def call(self, method, path, body=None, data=None, headers=None):
        """Send one request, print it, and return (status, parsed JSON)."""
        h = {"X-API-Key": self.key, **(headers or {})}
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=h, method=method)
        print(f">>> {method} {self.base + path}")
        if body is not None:
            print(json.dumps(body, indent=2))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        try:
            resp = json.loads(raw)
        except ValueError:
            resp = raw.decode(errors="replace")
        print(f"<<< HTTP {status}")
        print(json.dumps(resp, indent=2, ensure_ascii=False))
        return status, resp

    def event_body(self, token, direction, event_id=None, authorized_at=None):
        return {
            "event_id": event_id or str(uuid.uuid4()),
            "estate_code": self.estate_code,
            "qr_token": token,
            "direction": direction,
            "authorized_at": authorized_at or now(),
            "opened_at": now(),
        }


def token_of(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("entries", help="GET list QR aktif")
    s.add_argument("--upcoming", action="store_true",
                   help="include_upcoming=1 (cache offline 6 jam)")

    for name, hlp in (("validate", "POST validasi saja"),
                      ("scan", "validasi, lalu kirim laporan buka gate")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("value", help="isi scan: URL /verify/<token> atau token")
        s.add_argument("--exit", action="store_true", help="direction=exit")

    s = sub.add_parser("event", help="POST laporan buka gate manual")
    s.add_argument("token")
    s.add_argument("--exit", action="store_true")
    s.add_argument("--event-id", help="dari validasi; kosong = UUID baru")
    s.add_argument("--authorized-at", help="dari validasi; kosong = sekarang")

    s = sub.add_parser("cancel", help="POST batalkan izin")
    s.add_argument("event_id")
    s.add_argument("--reason", default="gate_failed",
                   choices=["gate_failed", "vehicle_reversed", "other"])

    s = sub.add_parser("batch", help="POST antrean laporan dari file JSON")
    s.add_argument("file", type=Path, help="JSON berisi list laporan")

    s = sub.add_parser("heartbeat", help="POST laporan kondisi PC")
    s.add_argument("--entry-gate", default="ok",
                   choices=["ok", "error", "offline"])
    s.add_argument("--exit-gate", default="ok",
                   choices=["ok", "error", "offline"])

    s = sub.add_parser("card", help="POST kartu RFID darurat")
    s.add_argument("card_uid")
    s.add_argument("--exit", action="store_true")
    s.add_argument("--token", help="qr_token opsional")

    s = sub.add_parser("capture", help="POST foto kendaraan (JPEG)")
    s.add_argument("jpeg", type=Path)
    s.add_argument("token")
    s.add_argument("--exit", action="store_true")

    a = p.parse_args(argv)
    try:
        c = Client(json.loads(a.config.read_text()))
    except (OSError, ValueError, KeyError) as e:
        sys.exit(f"Config error ({a.config}): {e}")
    direction = "exit" if getattr(a, "exit", False) else "entry"

    try:
        if a.cmd == "entries":
            q = "?include_upcoming=1" if a.upcoming else ""
            status, _ = c.call("GET", "/entries" + q)
        elif a.cmd in ("validate", "scan"):
            status, r = c.call("POST", "/validate",
                               {"value": a.value, "direction": direction})
            if a.cmd == "scan" and isinstance(r, dict) \
                    and r.get("decision") == "allowed":
                print("--- gate opened (simulated), sending report")
                status, _ = c.call("POST", "/events", c.event_body(
                    token_of(a.value), direction,
                    r["event_id"], r["authorized_at"]))
        elif a.cmd == "event":
            status, _ = c.call("POST", "/events", c.event_body(
                a.token, direction, a.event_id, a.authorized_at))
        elif a.cmd == "cancel":
            status, _ = c.call("POST", "/cancel",
                               {"event_id": a.event_id, "reason": a.reason})
        elif a.cmd == "batch":
            evs = json.loads(a.file.read_text())
            status, _ = c.call("POST", "/events/batch", {"events": evs})
        elif a.cmd == "heartbeat":
            status, _ = c.call("POST", "/heartbeat", {
                "agent_version": "0.1.0", "pc_time": now(),
                "cache_entries": 0, "pending_events": 0,
                "gates": {"entry": a.entry_gate, "exit": a.exit_gate}})
        elif a.cmd == "card":
            body = {"event_id": str(uuid.uuid4()), "direction": direction,
                    "card_type": "RFID", "card_uid": a.card_uid,
                    "opened_at": now()}
            if a.token:
                body["qr_token"] = a.token
            status, _ = c.call("POST", "/card-events", body)
        elif a.cmd == "capture":
            status, _ = c.call("POST", "/vehicle-capture",
                               data=a.jpeg.read_bytes(),
                               headers={"Content-Type": "image/jpeg",
                                        "X-QR-Token": a.token,
                                        "X-Gate-Direction": direction})
    except urllib.error.URLError as e:
        sys.exit(f"Connection failed: {e.reason}")
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
