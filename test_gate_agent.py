"""Self-check for gate_agent decisions. Run: python3 test_gate_agent.py"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gate_agent import GateAgent, QrCache, Reporter, UsedTokens

ENTRY = {"direction": "entry", "relay": 1}
EXIT = {"direction": "exit", "relay": 2}
ALLOWED = {"decision": "allowed", "reason": "sesuai_jadwal",
           "event_id": "e1", "authorized_at": "2026-11-12T00:00:00.000Z"}
DOWN = OSError("down")


def ts(minutes):
    t = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return t.isoformat().replace("+00:00", "Z")


def entries(*items, valid_for=360):
    return (200, {"cache_valid_until": ts(valid_for), "entries": list(items)})


class FakeApi:
    """Answers by path; an exception instance is raised instead."""

    def __init__(self, **answers):
        self.answers = {"/validate": (200, ALLOWED),
                        "/events": (200, {"ok": True, "accepted": True}),
                        "/cancel": (200, {"ok": True, "released": True}),
                        "/entries?include_upcoming=1": entries(),
                        **answers}
        self.calls, self.reports = [], []

    def call(self, method, path, body=None):
        self.calls.append(path)
        if path == "/events":
            self.reports.append(body)
        ans = self.answers[path]
        if isinstance(ans, Exception):
            raise ans
        return ans

    def event_body(self, token, direction, event_id=None, authorized_at=None):
        return {"event_id": event_id or "new-uuid", "qr_token": token,
                "direction": direction}


class FakeOpener:
    def __init__(self, outcome="opened"):
        self.outcome, self.opened = outcome, 0

    def open(self, gate):
        self.opened += 1
        return self.outcome


def make(tmp, api, outcome="opened"):
    tmp = Path(tmp)
    opener = FakeOpener(outcome)
    agent = GateAgent(api, opener, UsedTokens(tmp / "used.json"),
                      Reporter(api, tmp / "pending.jsonl"),
                      QrCache(tmp / "cache.json"))
    return agent, opener


def test_online():
    with tempfile.TemporaryDirectory() as d:
        # allowed: opens, reports, then a re-scan is blocked locally
        api = FakeApi()
        agent, opener = make(d, api)
        r = agent.handle_scan(ENTRY, "https://x/verify/tok1")
        assert r.opened and opener.opened == 1, r
        assert api.calls == ["/validate", "/events"], api.calls
        assert agent.handle_scan(ENTRY, "tok1").code == "used_locally"
        # entry record does not block the exit of the same token
        assert agent.handle_scan(EXIT, "tok1").opened
        assert agent.handle_scan(EXIT, "tok1").code == "used_locally"
        # used tokens survive a restart
        agent2, _ = make(d, api)
        assert agent2.handle_scan(ENTRY, "tok1").code == "used_locally"

    with tempfile.TemporaryDirectory() as d:
        # rejected: no open, reason passed through, cache never consulted
        api = FakeApi(**{"/validate": (200, {"decision": "rejected",
                                             "reason": "qr_sudah_masuk"})})
        agent, opener = make(d, api)
        agent.cache.data = entries({"qr_token": "tok2",
                                    "valid_until": ts(60)})[1]
        r = agent.handle_scan(ENTRY, "tok2")
        assert not r.opened and r.code == "qr_sudah_masuk"
        assert opener.opened == 0
        agent, _ = make(d, FakeApi(**{"/validate": (401, {})}))
        assert agent.handle_scan(ENTRY, "tok2").code == "api_error_401"


def test_relay_outcomes():
    with tempfile.TemporaryDirectory() as d:
        # relay failed: cancel, token released for a re-scan
        api = FakeApi()
        agent, _ = make(d, api, "failed")
        r = agent.handle_scan(ENTRY, "tok4")
        assert r.code == "gate_failed" and "/cancel" in api.calls, api.calls
        assert not agent.used.has("entry", "tok4")
        # relay outcome unknown: token stays used, no report, no cancel
        api = FakeApi()
        agent, _ = make(d, api, "unknown")
        r = agent.handle_scan(ENTRY, "tok5")
        assert r.code == "gate_unknown" and agent.used.has("entry", "tok5")
        assert api.calls == ["/validate"], api.calls


def test_offline():
    with tempfile.TemporaryDirectory() as d:
        api = FakeApi(**{"/entries?include_upcoming=1": entries(
            {"qr_token": "ok", "valid_from": ts(-10), "valid_until": ts(60)},
            {"qr_token": "early", "valid_from": ts(10), "valid_until": ts(60)},
            {"qr_token": "late", "valid_from": ts(-60), "valid_until": ts(-1)},
        )})
        agent, opener = make(d, api)
        agent.sync()
        for ans in (DOWN, (502, {})):  # network error and 5xx both fall back
            api.answers["/validate"] = ans
            api.answers["/events"] = DOWN
            assert agent.handle_scan(ENTRY, "early").code == "offline_too_early"
            assert agent.handle_scan(ENTRY, "late").code == "offline_too_late"
            assert agent.handle_scan(ENTRY, "new").code == \
                "offline_not_in_cache"
            assert agent.handle_scan(EXIT, "never").code == \
                "offline_not_entered"
        r = agent.handle_scan(ENTRY, "ok")
        assert r.opened and r.code == "offline_cache", r
        # offline report uses a fresh event_id and is queued
        assert api.reports[-1]["event_id"] == "new-uuid"
        pending = Path(d) / "pending.jsonl"
        assert "new-uuid" in pending.read_text()
        # exit opens offline because this Pi recorded the entry
        assert agent.handle_scan(EXIT, "ok").code == "offline_exit"
        # cache survives a restart
        agent2, _ = make(d, api)
        agent2.api.answers["/validate"] = DOWN
        assert agent2.cache.check("ok") == "allowed"

        # server back: sync refreshes cache and flushes the queue
        api.answers["/events/batch"] = (200, {"ok": True, "results": [
            {"event_id": "new-uuid", "ok": True}]})
        agent.sync()
        assert pending.read_text() == ""

    with tempfile.TemporaryDirectory() as d:
        # no cache or expired cache: deny
        api = FakeApi(**{"/validate": DOWN})
        agent, _ = make(d, api)
        assert agent.handle_scan(ENTRY, "x").code == "offline_cache_expired"
        agent.cache.data = entries({"qr_token": "x", "valid_until": ts(60)},
                                   valid_for=-1)[1]
        assert agent.handle_scan(ENTRY, "x").code == "offline_cache_expired"
        # failed sync keeps the previous list
        agent.cache.data = entries({"qr_token": "x", "valid_until": ts(60)})[1]
        api.answers["/entries?include_upcoming=1"] = DOWN
        agent.sync()
        assert agent.cache.check("x") == "allowed"

    with tempfile.TemporaryDirectory() as d:
        # offline relay failure: unmark, but no cancel (server never knew)
        api = FakeApi(**{"/validate": DOWN})
        agent, _ = make(d, api, "failed")
        agent.cache.data = entries({"qr_token": "x", "valid_until": ts(60)})[1]
        assert agent.handle_scan(ENTRY, "x").code == "gate_failed"
        assert "/cancel" not in api.calls
        assert not agent.used.has("entry", "x")


if __name__ == "__main__":
    test_online()
    test_relay_outcomes()
    test_offline()
    print("all checks passed")
