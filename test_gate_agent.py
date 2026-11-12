"""Self-check for gate_agent decisions. Run: python3 test_gate_agent.py"""

import tempfile
from pathlib import Path

from gate_agent import GateAgent, Reporter, UsedTokens

GATE = {"direction": "entry", "relay": 1}
ALLOWED = {"decision": "allowed", "reason": "sesuai_jadwal",
           "event_id": "e1", "authorized_at": "2026-11-12T00:00:00.000Z"}


class FakeApi:
    """Answers by path; an exception instance is raised instead."""
    estate_code = "DEMO"

    def __init__(self, **answers):
        self.answers = {"/validate": (200, ALLOWED),
                        "/events": (200, {"ok": True, "accepted": True}),
                        "/cancel": (200, {"ok": True, "released": True}),
                        **answers}
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append(path)
        ans = self.answers[path]
        if isinstance(ans, Exception):
            raise ans
        return ans

    def event_body(self, token, direction, event_id, authorized_at):
        return {"event_id": event_id, "qr_token": token}


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
                      Reporter(api, tmp / "pending.jsonl"))
    return agent, opener


def test_all():
    with tempfile.TemporaryDirectory() as d:
        # allowed: opens, reports, then a re-scan is blocked locally
        api = FakeApi()
        agent, opener = make(d, api)
        r = agent.handle_scan(GATE, "https://x/verify/tok1")
        assert r.opened and opener.opened == 1, r
        assert api.calls == ["/validate", "/events"], api.calls
        r = agent.handle_scan(GATE, "tok1")
        assert r.code == "used_locally" and opener.opened == 1, r
        # used tokens survive a restart
        agent2, _ = make(d, api)
        assert agent2.handle_scan(GATE, "tok1").code == "used_locally"

    with tempfile.TemporaryDirectory() as d:
        # rejected: no open, reason passed through
        api = FakeApi(**{"/validate": (200, {"decision": "rejected",
                                             "reason": "qr_sudah_masuk"})})
        agent, opener = make(d, api)
        r = agent.handle_scan(GATE, "tok2")
        assert not r.opened and r.code == "qr_sudah_masuk"
        assert opener.opened == 0

    with tempfile.TemporaryDirectory() as d:
        # server down or 5xx: no open
        for ans in (OSError("down"), (500, {"code": "SERVER_ERROR"})):
            agent, opener = make(d, FakeApi(**{"/validate": ans}))
            r = agent.handle_scan(GATE, "tok3")
            assert r.code == "server_unreachable" and opener.opened == 0, r
        agent, _ = make(d, FakeApi(**{"/validate": (401, {})}))
        assert agent.handle_scan(GATE, "tok3").code == "api_error_401"

    with tempfile.TemporaryDirectory() as d:
        # relay failed: cancel, token released for a re-scan
        api = FakeApi()
        agent, _ = make(d, api, "failed")
        r = agent.handle_scan(GATE, "tok4")
        assert r.code == "gate_failed" and "/cancel" in api.calls, api.calls
        assert "tok4" not in agent.used
        # relay outcome unknown: token stays used, no report, no cancel
        api = FakeApi()
        agent, _ = make(d, api, "unknown")
        r = agent.handle_scan(GATE, "tok5")
        assert r.code == "gate_unknown" and "tok5" in agent.used
        assert api.calls == ["/validate"], api.calls

    with tempfile.TemporaryDirectory() as d:
        # report fails: queued, then flushed by batch once server is back
        api = FakeApi(**{"/events": OSError("down"),
                         "/events/batch": OSError("down")})
        agent, _ = make(d, api)
        assert agent.handle_scan(GATE, "tok6").opened
        pending = Path(d) / "pending.jsonl"
        assert "e1" in pending.read_text()
        api.answers["/events/batch"] = (200, {"ok": True, "results": [
            {"event_id": "e1", "ok": True}]})
        agent.reporter.flush()
        assert pending.read_text() == ""


if __name__ == "__main__":
    test_all()
    print("all checks passed")
