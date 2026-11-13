#!/usr/bin/env python3
"""Gate agent: QR scan -> server validation -> open barrier -> report.

Runs on the Pi and wires three parts together:
  scanner  USB keyboard-wedge QR reader, read with evdev
  server   PC QR API, through gate_client.Client
  barrier  ESP32-C3 relay, through esp32c3-relay's RelayClient

When the server is unreachable, entry scans fall back to a QR list cached
by periodic sync. Rules follow api-contract/pc-qr-api-guide.txt sections 1
to 5. See README "Gate agent" for the flow and where to extend it.

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
from datetime import datetime, timedelta, timezone
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
    token: str = ""


def parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def write_atomic(path: Path, text: str):
    """Replace path so a power cut leaves either the old or new file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path: Path, default):
    """Read a state file. A corrupt one is set aside, not fatal.

    Crashing here would make systemd restart the agent forever and keep
    the barrier shut, so the agent starts with `default` instead.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        bad = path.with_suffix(path.suffix + ".corrupt")
        log.error("%s unreadable (%s), moved to %s", path, e, bad.name)
        os.replace(path, bad)
        return default


# Local state -------------------------------------------------------------

class UsedTokens:
    """Tokens that opened a barrier, per direction, kept across restarts.

    The server re-allows a token if the open report is late, so the Pi
    must reject re-scans itself (guide section 2). The entry record also
    lets an exit open offline (guide "SKENARIO GANGGUAN").
    """

    def __init__(self, path: Path, keep_days=7):
        self.path = path
        self.keep = timedelta(days=keep_days)
        data = load_json(path, {})
        self._used = {"entry": data.get("entry", {}),
                      "exit": data.get("exit", {})}
        self._prune()

    def has(self, direction, token):
        return token in self._used[direction]

    def add(self, direction, token):
        self._used[direction][token] = now()
        self._save()

    def discard(self, direction, token):
        if self._used[direction].pop(token, None):
            self._save()

    def _prune(self):
        """Forget tokens older than keep_days. An old entry record no
        longer lets that vehicle exit offline; online exit is unaffected."""
        cutoff = datetime.now(timezone.utc) - self.keep
        for d in self._used:
            self._used[d] = {t: at for t, at in self._used[d].items()
                             if parse_ts(at) >= cutoff}

    def _save(self):
        self._prune()
        write_atomic(self.path, json.dumps(self._used, indent=1))


class QrCache:
    """Last QR list from GET /entries?include_upcoming=1 (guide section 1).

    Entry scans are decided from this list first; the server is asked only
    for a QR not in it. If the server is then unreachable, the QR is
    rejected (e.g. registered after the last sync).
    """

    def __init__(self, path: Path):
        self.path = path
        self.data = load_json(path, {})

    def refresh(self, api: Client):
        """Pull a fresh list. Returns True on success; keeps the old list
        otherwise."""
        try:
            status, resp = api.call("GET", "/entries?include_upcoming=1")
        except OSError as e:
            log.warning("cache sync failed: %r", e)
            return False
        if status != 200 or not isinstance(resp, dict) \
                or not isinstance(resp.get("entries"), list):
            log.warning("cache sync failed: HTTP %s", status)
            return False
        self.data = resp
        write_atomic(self.path, json.dumps(resp, indent=1))
        log.info("cache synced: %d entries, valid until %s",
                 len(resp["entries"]), resp.get("cache_valid_until"))
        return True

    def check(self, token, at=None):
        """Return "allowed" or the offline rejection code."""
        at = at or datetime.now(timezone.utc)
        until = self.data.get("cache_valid_until")
        if not until or at >= parse_ts(until):
            return "offline_cache_expired"
        for e in self.data.get("entries", []):
            if e.get("qr_token") != token:
                continue
            if e.get("valid_from") and at < parse_ts(e["valid_from"]):
                return "slot_too_early"
            if at >= parse_ts(e["valid_until"]):
                return "slot_too_late"
            return "allowed"
        return "offline_not_in_cache"


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
                f.flush()
                os.fsync(f.fileno())

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
        queued = []
        for line in self.path.read_text().splitlines():
            try:
                queued.append(json.loads(line))
            except ValueError:
                # a power cut mid-append leaves a partial last line
                if line.strip():
                    log.error("dropping unreadable queued report: %r", line)
        if not queued:
            return
        try:
            status, resp = self.api.call("POST", "/events/batch",
                                         {"events": queued[:200]})
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

    validate_local()    entry decision from the cached slot, tried first
    validate()          server decision when the cache cannot tell
    validate_offline()  fallback decision when the server is unreachable
    on_result()         add buzzer, LED or display feedback here
    """

    def __init__(self, api: Client, opener, used: UsedTokens,
                 reporter: Reporter, cache: QrCache):
        self.api = api
        self.opener = opener
        self.used = used
        self.reporter = reporter
        self.cache = cache
        # ponytail: one scan at a time across gates, per-gate locks if slow
        self._lock = threading.Lock()

    def handle_scan(self, gate, value):
        with self._lock:
            result = self._handle(gate, value)
            result.token = token_of(value.strip())
            self.on_result(gate, result)
        return result

    def sync(self):
        """Periodic job: refresh the QR cache, then resend queued reports."""
        if self.cache.refresh(self.api):
            with self._lock:
                self.reporter.flush()

    def _handle(self, gate, value):
        direction = gate["direction"]
        token = token_of(value.strip())
        log.info("[%s] scanned qr=%s", direction, token[:8] or "-")
        if not token:
            return Result(False, "empty_scan")
        if self.used.has(direction, token):
            return Result(False, "used_locally")

        code, resp, via = None, {}, "cache"
        if direction == "entry":
            code, resp = self.validate_local(token)
        if code is None:
            via = "server"
            code, resp = self.validate(value, direction)
            if code == "server_unreachable":
                via = "cache"
                code, resp = self.validate_offline(token, direction)
        log.info("[%s] match qr=%s via %s: %s",
                 direction, token[:8], via, code)
        if code != "allowed":
            return Result(False, code, resp)

        # Mark before opening so a re-scan cannot pass (guide section 3).
        self.used.add(direction, token)
        outcome = self.opener.open(gate)
        if outcome == "opened":
            log.info("[%s] gate opened: relay %s qr=%s", direction,
                     gate["relay"], token[:8])
        if outcome == "failed":
            self.used.discard(direction, token)
            if resp.get("event_id"):
                self.cancel(resp["event_id"], "gate_failed")
            return Result(False, "gate_failed", resp)
        if outcome == "unknown":
            return Result(False, "gate_unknown", resp)

        # Local decision: new event_id and local authorized_at (section 3).
        # ponytail: report blocks the next scan up to timeout_s; thread it
        self.reporter.send(self.api.event_body(
            token, direction, resp.get("event_id"), resp.get("authorized_at")))
        return Result(True, resp.get("reason", "allowed"), resp)

    def validate(self, value, direction):
        """Ask the server. Returns (code, response).

        code is the decision ("allowed", "payment_required"), the server's
        reason for "rejected", "server_unreachable" (network error or 5xx,
        cache may decide), or "api_error_<status>" (config problem, never
        overridden by cache).
        """
        try:
            status, resp = self.api.call(
                "POST", "/validate", {"value": value, "direction": direction})
        except OSError as e:
            log.error("server unreachable: %r", e)
            return "server_unreachable", {}
        if status >= 500:
            log.error("server error: HTTP %s", status)
            return "server_unreachable", {}
        if status != 200 or not isinstance(resp, dict):
            return f"api_error_{status}", resp if isinstance(resp, dict) else {}
        decision = resp.get("decision", "unknown")
        if decision == "rejected":
            return resp.get("reason", "rejected"), resp
        return decision, resp

    def validate_local(self, token):
        """Decide an entry from the cached slot, without the server.

        Returns (None, {}) when the cache cannot tell (QR not cached, or
        no valid list), so the server is asked instead.
        """
        code = self.cache.check(token)
        if code == "allowed":
            return "allowed", {"reason": "local_slot"}
        if code in ("slot_too_early", "slot_too_late"):
            return code, {}
        return None, {}

    def validate_offline(self, token, direction):
        """Decide without the server (guide "SKENARIO GANGGUAN").

        Entry: validate_local() already failed, so only the deny reason is
        left. Exit: token must have entered through this Pi.
        """
        if direction == "exit":
            if self.used.has("entry", token):
                return "allowed", {"reason": "offline_exit"}
            return "offline_not_entered", {}
        return self.cache.check(token), {}

    def cancel(self, event_id, reason):
        try:
            self.api.call("POST", "/cancel",
                          {"event_id": event_id, "reason": reason})
        except OSError as e:
            log.error("cancel %s failed: %r", event_id, e)

    def on_result(self, gate, result):
        # 8 chars identify the QR in logs without exposing the full token
        level = logging.INFO if result.opened else logging.WARNING
        log.log(level, "[%s] %s: %s qr=%s %s", gate["direction"],
                "OPEN" if result.opened else "DENY", result.code,
                result.token[:8] or "-",
                result.response.get("tracking_code", ""))


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


def run_sync(agent, every_s):
    """Periodic sync. main() already ran the first one at startup."""
    while True:
        time.sleep(every_s)
        try:
            agent.sync()
        except Exception:
            log.exception("sync crashed")


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
    agent = GateAgent(api, opener,
                      UsedTokens(state / "used_tokens.json",
                                 agent_cfg.get("used_token_days", 7)),
                      Reporter(api, state / "pending_reports.jsonl"),
                      QrCache(state / "qr_cache.json"))

    # Fill the cache before accepting scans; on failure the old file stays.
    log.info("startup sync: downloading QR list")
    try:
        agent.sync()
    except Exception:
        log.exception("startup sync crashed")

    threading.Thread(target=run_sync, daemon=True, args=(
        agent, agent_cfg.get("sync_interval_s", 60))).start()

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
