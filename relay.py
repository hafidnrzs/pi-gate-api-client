#!/usr/bin/env python3
"""Client for the KITB-Relay ESP32-C3 (JSON lines over USB CDC).

Only the reader thread opens and closes the serial port. Other threads ask
it to reopen through a flag, so a port is never closed under a running
readline() (pyserial then raises TypeError and kills the reader).

* Replies are matched to waiting requests by `id`.
* Pulses are retried with the SAME id; the firmware de-duplicates, so a
  retry can never fire the relay twice.
* The reader never dies: any error reopens the port with a short backoff.

Usage:
  python3 relay.py [PORT] ping
  python3 relay.py [PORT] pulse RELAY [MS]
"""
import itertools
import json
import logging
import queue
import random
import threading
import time

import serial  # apt install python3-serial

log = logging.getLogger("relay")


class RelayError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class RelayTimeout(TimeoutError):
    pass


class RelayClient:
    PING_TIMEOUT = 0.5
    MISSES_BEFORE_OFFLINE = 3
    REOPEN_DELAY = 1.0

    def __init__(self, port="/dev/ttyACM0", opener=None, ping_interval=3.0):
        self.port = port
        self._open = opener or (lambda: serial.Serial(
            port, 115200, timeout=0.1, exclusive=True))
        self._ser = None
        self._reopen = False                   # set by writers, read by reader
        self._open_failed = False
        self._lock = threading.Lock()          # guards writes and _waiters
        self._waiters = {}                     # id -> queue.Queue
        # Random start: avoids colliding with ids the firmware still remembers.
        self._ids = itertools.count(random.randint(1, 2**31))
        self._stop = threading.Event()
        self.online = False
        self.last_pong = None
        self._misses = 0
        self._ping_interval = ping_interval
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._monitor = threading.Thread(target=self._monitor_loop,
                                         daemon=True)

    # lifecycle -------------------------------------------------------------
    def start(self):
        self._ser = self._try_open()  # sync, so a ping right after start works
        self._reader.start()
        if self._ping_interval:
            self._monitor.start()
        return self

    def close(self):
        self._stop.set()
        if self._reader.is_alive():
            self._reader.join(timeout=2)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def _try_open(self):
        try:
            ser = self._open()
        except Exception as e:
            if not self._open_failed:  # log once, not every retry
                log.warning("open %s failed: %s", self.port, e)
            self._open_failed = True
            return None
        if self._open_failed:
            log.info("reopened %s", self.port)
        self._open_failed = False
        return ser

    def _drop_port(self):
        """Reader thread only."""
        with self._lock:
            ser, self._ser = self._ser, None
        if ser:
            try:
                ser.close()
            except Exception:
                pass

    # io --------------------------------------------------------------------
    def _read_loop(self):
        try:
            while not self._stop.is_set():
                if self._reopen:
                    self._reopen = False
                    self._drop_port()
                if self._ser is None:
                    if self._stop.wait(self.REOPEN_DELAY):
                        break
                    ser = self._try_open()
                    with self._lock:
                        self._ser = ser
                    continue
                try:
                    raw = self._ser.readline()
                except Exception as e:
                    log.warning("read %s failed: %r", self.port, e)
                    self._drop_port()
                    continue
                if raw:
                    self._dispatch(raw)
        finally:
            self._drop_port()

    def _dispatch(self, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        with self._lock:
            q = self._waiters.get(msg.get("id"))
        if q is not None:
            q.put(msg)

    def _send(self, payload):
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        with self._lock:
            if self._ser is None:
                raise RelayTimeout("port closed")
            try:
                self._ser.write(data)
            except Exception as e:
                self._reopen = True
                raise RelayTimeout(f"write failed: {e!r}")

    def _transact(self, payload, final, timeout, attempts=1):
        rid = next(self._ids)
        payload = dict(payload, id=rid)
        q = queue.Queue()
        with self._lock:
            self._waiters[rid] = q
        try:
            last = RelayTimeout("no attempt made")
            for _ in range(attempts):
                try:
                    self._send(payload)
                except RelayTimeout as e:
                    last = e
                    time.sleep(0.2)
                    continue
                deadline = time.monotonic() + timeout
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        last = RelayTimeout(
                            f"no {final} for {payload['cmd']} id={rid}")
                        break
                    try:
                        msg = q.get(timeout=left)
                    except queue.Empty:
                        continue
                    if not msg.get("ok"):
                        raise RelayError(msg.get("err", "UNKNOWN"))
                    if msg.get("evt") == final:
                        return msg
            raise last
        finally:
            with self._lock:
                self._waiters.pop(rid, None)

    # api -------------------------------------------------------------------
    def ping(self):
        msg = self._transact({"cmd": "ping"}, "pong", self.PING_TIMEOUT)
        self.last_pong = msg
        return msg

    def pulse(self, relay, ms=1000, attempts=2):
        """Fire a relay for `ms`; returns the 'done' message. Safe to retry."""
        return self._transact({"cmd": "pulse", "relay": relay, "ms": ms},
                              "done", ms / 1000 + 1.0, attempts)

    # health ----------------------------------------------------------------
    def _monitor_loop(self):
        while not self._stop.wait(self._ping_interval):
            try:
                pong = self.ping()
                self._misses = 0
                if not self.online:
                    log.info("device online (reset=%s)", pong.get("reset"))
                self.online = True
            except (RelayTimeout, RelayError):
                self._misses += 1
                if self._misses >= self.MISSES_BEFORE_OFFLINE:
                    if self.online:
                        log.error("device offline")
                    self.online = False
                    self._misses = 0
                    self._reopen = True


def _demo():
    """Self-check with a fake port: a close under readline must not kill
    the reader, and the client must recover on the reopened port."""

    class FakePort:
        def __init__(self):
            self.fd, self.out = 1, queue.Queue()

        def readline(self):
            time.sleep(0.01)
            if self.fd is None:
                raise TypeError("'NoneType' object cannot be interpreted")
            try:
                return self.out.get_nowait()
            except queue.Empty:
                return b""

        def write(self, data):
            if self.fd is None:
                raise serial.SerialException("closed")
            msg = json.loads(data)
            evt = "pong" if msg["cmd"] == "ping" else "done"
            self.out.put(json.dumps(
                {"id": msg["id"], "ok": True, "evt": evt}).encode())

        def close(self):
            self.fd = None

    ports = []

    def opener():
        ports.append(FakePort())
        return ports[-1]

    RelayClient.REOPEN_DELAY = 0.05
    c = RelayClient("fake", opener, ping_interval=0).start()
    assert c.ping()["evt"] == "pong"
    ports[-1].close()  # yank the port from under the reader
    time.sleep(0.3)
    assert c._reader.is_alive(), "reader died"
    assert c.pulse(1, 10)["evt"] == "done"
    assert len(ports) == 2, len(ports)
    c.close()
    assert not c._reader.is_alive() and ports[-1].fd is None
    print("relay self-check ok")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    args = sys.argv[1:]
    if args == ["--self-check"]:
        _demo()
        sys.exit()
    port = args.pop(0) if args and args[0].startswith("/") else "/dev/ttyACM0"
    cmd = args[0] if args else "ping"
    with RelayClient(port, ping_interval=0) as c:
        if cmd == "pulse":
            print(c.pulse(int(args[1]), int(args[2]) if len(args) > 2 else 1000))
        else:
            print(c.ping())
