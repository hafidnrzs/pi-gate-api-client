#!/usr/bin/env python3
"""Gate agent: QR scan -> server validation -> open barrier -> report.

Runs on the Pi and wires three parts together:
  scanner  USB keyboard-wedge QR reader, read with evdev
  server   PC QR API, through gate_client.Client
  barrier  ESP32-C3 relay, through esp32c3-relay's RelayClient

Rules follow api-contract/pc-qr-api-guide.txt sections 2 to 5. See README
"Gate agent" for the flow and where to extend it.

Usage:
  python3 gate_agent.py                     # real scanners and relay
  python3 gate_agent.py --stdin --dry-run   # type scans, no relay
"""

import argparse
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from gate_client import Client, now, token_of

log = logging.getLogger("gate")
HERE = Path(__file__).resolve().parent
DROP_STATUSES = (400, 404, 409)  # server will never accept these reports


@dataclass
class Result:
    """Outcome of one scan. `code` is a short reason for logs and feedback."""
    opened: bool
    code: str
    response: dict = field(default_factory=dict)


# Local state -------------------------------------------------------------

class UsedTokens:
    """Tokens whose barrier already opened, persisted across restarts.

    The server re-allows a token if the open report is late, so the Pi
    must reject re-scans itself (guide section 2).
    """

    def __init__(self, path: Path):
        self.path = path
        self._tokens = json.loads(path.read_text()) if path.exists() else {}

    def __contains__(self, token):
        return token in self._tokens

    def add(self, token):
        self._tokens[token] = now()
        self._save()

    def discard(self, token):
        if self._tokens.pop(token, None):
            self._save()

    def _save(self):
        # ponytail: grows forever, prune old entries if the file gets big
        write_atomic(self.path, json.dumps(self._tokens, indent=1))


class Reporter:
    """Sends open reports. Failed ones are queued to a JSONL file and
    retried with the same event_id, so the server counts each once."""

    def __init__(self, api: Client, path: Path):
        self.api = api
        self.path = path

    def send(self, body):
        if not self._post(body):
            log.warning("report %s queued for retry", body["event_id"])
            with self.path.open("a") as f:
                f.write(json.dumps(body) + "\n")

    def _post(self, body):
        """Return True when the report needs no retry."""
        try:
            status, resp = self.api.call("POST", "/events", body)
        except OSError:
            return False
        if status == 200:
            if not resp.get("accepted"):
                log.warning("report %s not accepted: %s",
                            body["event_id"], resp.get("reason"))
            return True
        if status in DROP_STATUSES:
            log.error("report %s dropped: HTTP %s %s",
                      body["event_id"], status, resp)
            return True
        return False

    def flush(self):
        """Resend queued reports in one batch (max 200, guide section 5)."""
        if not self.path.exists():
            return
        queued = [json.loads(x) for x in self.path.read_text().splitlines()
                  if x.strip()]
        if not queued:
            return
        batch = queued[:200]
        try:
            status, resp = self.api.call("POST", "/events/batch",
                                         {"events": batch})
        except OSError:
            return
        if status != 200:
            return
        done = {r.get("event_id") for r in resp.get("results", [])
                if r.get("ok") or r.get("status") in DROP_STATUSES}
        left = [e for e in queued if e["event_id"] not in done]
        write_atomic(self.path, "".join(json.dumps(e) + "\n" for e in left))
        log.info("flushed %d queued reports, %d left",
                 len(queued) - len(left), len(left))


def write_atomic(path: Path, text: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# Barrier -----------------------------------------------------------------

class RelayOpener:
    """Opens a barrier by pulsing its relay on the ESP32-C3.

    open() returns "opened", "failed" (relay surely did not fire) or
    "unknown" (it may have fired, so the token stays marked as used).
    """

    def __init__(self, port, driver_dir):
        sys.path.insert(0, str(Path(driver_dir).expanduser()))
        from test_relay import RelayClient, RelayError  # esp32c3-relay/pi
        self._relay_error = RelayError
        self.relay = RelayClient(port, ping_interval=0).start()

    def open(self, gate):
        try:
            self.relay.ping()
        except Exception as e:
            log.error("relay not responding: %r", e)
            return "failed"
        try:
            self.relay.pulse(gate["relay"], gate.get("pulse_ms", 1000))
            return "opened"
        except self._relay_error as e:
            log.error("relay refused pulse: %s", e.code)
            return "failed"
        except Exception as e:
            log.error("relay outcome unknown: %r", e)
            return "unknown"


class DryRunOpener:
    """Pretends to open the barrier. For testing without hardware."""

    def open(self, gate):
        log.info("[dry-run] would pulse relay %s for %s ms",
                 gate["relay"], gate.get("pulse_ms", 1000))
        return "opened"


# Scan handling -----------------------------------------------------------

class GateAgent:
    """Decides what to do with one scan. Extension points:

    validate()  add cache fallback here when the server is unreachable
    on_result() add buzzer, LED or display feedback here
    """

    def __init__(self, api: Client, opener, used: UsedTokens,
                 reporter: Reporter):
        self.api = api
        self.opener = opener
        self.used = used
        self.reporter = reporter
        # ponytail: one scan at a time across gates, per-gate locks if slow
        self._lock = threading.Lock()

    def handle_scan(self, gate, value):
        with self._lock:
            result = self._handle(gate, value)
            self.on_result(gate, result)
            self.reporter.flush()
        return result

    def _handle(self, gate, value):
        token = token_of(value.strip())
        if not token:
            return Result(False, "empty_scan")
        if token in self.used:
            return Result(False, "used_locally")

        code, resp = self.validate(value, gate["direction"])
        if code != "allowed":
            return Result(False, code, resp)

        # Mark before opening so a re-scan cannot pass (guide section 3).
        self.used.add(token)
        outcome = self.opener.open(gate)
        if outcome == "failed":
            self.used.discard(token)
            self.cancel(resp["event_id"], "gate_failed")
            return Result(False, "gate_failed", resp)
        if outcome == "unknown":
            return Result(False, "gate_unknown", resp)

        self.reporter.send(self.api.event_body(
            token, gate["direction"], resp["event_id"], resp["authorized_at"]))
        return Result(True, resp.get("reason", "allowed"), resp)

    def validate(self, value, direction):
        """Ask the server. Returns (code, response).

        code is the decision ("allowed", "rejected", "payment_required"),
        "server_unreachable" (network error or 5xx, cache may decide), or
        "api_error_<status>" (config problem, never overridden by cache).
        For "rejected", code is the server's reason instead.
        """
        try:
            status, resp = self.api.call(
                "POST", "/validate", {"value": value, "direction": direction})
        except OSError as e:
            log.error("server unreachable: %r", e)
            return "server_unreachable", {}
        if status >= 500:
            return "server_unreachable", {}
        if status != 200 or not isinstance(resp, dict):
            return f"api_error_{status}", resp if isinstance(resp, dict) else {}
        decision = resp.get("decision", "unknown")
        if decision == "rejected":
            return resp.get("reason", "rejected"), resp
        return decision, resp

    def cancel(self, event_id, reason):
        try:
            self.api.call("POST", "/cancel",
                          {"event_id": event_id, "reason": reason})
        except OSError as e:
            log.error("cancel %s failed: %r", event_id, e)

    def on_result(self, gate, result):
        level = logging.INFO if result.opened else logging.WARNING
        log.log(level, "[%s] %s: %s", gate["direction"],
                "OPEN" if result.opened else "DENY", result.code)


# Scanner -----------------------------------------------------------------

# US keyboard scancode -> (normal, shifted), enough for /verify/<token> URLs
SCANCODES = {
    2: "1!", 3: "2@", 4: "3#", 5: "4$", 6: "5%", 7: "6^", 8: "7&", 9: "8*",
    10: "9(", 11: "0)", 12: "-_", 13: "=+",
    16: "qQ", 17: "wW", 18: "eE", 19: "rR", 20: "tT", 21: "yY", 22: "uU",
    23: "iI", 24: "oO", 25: "pP", 26: "[{", 27: "]}",
    30: "aA", 31: "sS", 32: "dD", 33: "fF", 34: "gG", 35: "hH", 36: "jJ",
    37: "kK", 38: "lL", 39: ";:", 40: "'\"", 41: "`~", 43: "\\|",
    44: "zZ", 45: "xX", 46: "cC", 47: "vV", 48: "bB", 49: "nN", 50: "mM",
    51: ",<", 52: ".>", 53: "/?", 57: "  ",
}
KEY_ENTER, SHIFT_KEYS = 28, (42, 54)


def evdev_scans(path):
    """Yield scanned strings from a keyboard-wedge scanner, forever.

    grab() keeps scans from also typing into the Pi console. Reconnects
    when the scanner is unplugged.
    """
    from evdev import InputDevice, ecodes
    while True:
        try:
            dev = InputDevice(path)
            dev.grab()
            log.info("scanner ready: %s", path)
            buf, shift = [], False
            for ev in dev.read_loop():
                if ev.type != ecodes.EV_KEY:
                    continue
                if ev.code in SHIFT_KEYS:
                    shift = ev.value != 0
                elif ev.value == 1 and ev.code == KEY_ENTER:
                    if buf:
                        yield "".join(buf)
                    buf = []
                elif ev.value == 1 and ev.code in SCANCODES:
                    buf.append(SCANCODES[ev.code][shift])
        except OSError as e:
            log.error("scanner %s: %s, retrying in 5 s", path, e)
            time.sleep(5)


def run_gate(agent, gate):
    for value in evdev_scans(gate["scanner"]):
        try:
            agent.handle_scan(gate, value)
        except Exception:
            log.exception("[%s] scan handling crashed", gate["direction"])


# Main ----------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", type=Path, default=HERE / "config.json")
    p.add_argument("--stdin", action="store_true",
                   help="read scans from stdin instead of scanners")
    p.add_argument("--gate", type=int, default=0,
                   help="gate index used with --stdin (default 0)")
    p.add_argument("--dry-run", action="store_true",
                   help="do not touch the relay")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print every API request and response")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads(a.config.read_text())
    agent_cfg = cfg["agent"]
    gates = agent_cfg["gates"]

    state = HERE / agent_cfg.get("state_dir", "state")
    state.mkdir(parents=True, exist_ok=True)
    api = Client(cfg, verbose=a.verbose)
    opener = DryRunOpener() if a.dry_run else RelayOpener(
        agent_cfg["relay_port"], agent_cfg["relay_driver_dir"])
    reporter = Reporter(api, state / "pending_reports.jsonl")
    agent = GateAgent(api, opener, UsedTokens(state / "used_tokens.json"),
                      reporter)
    reporter.flush()

    if a.stdin:
        gate = gates[a.gate]
        log.info("reading scans from stdin for gate %s", gate["direction"])
        for line in sys.stdin:
            if line.strip():
                agent.handle_scan(gate, line.strip())
        return

    threads = [threading.Thread(target=run_gate, args=(agent, g), daemon=True)
               for g in gates]
    for t in threads:
        t.start()
    log.info("gate agent running with %d gate(s)", len(threads))
    for t in threads:
        t.join()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
