# pi-gate-api-client

Raspberry Pi software for an estate gate: a QR scan is validated against
the estate web server and, if allowed, the barrier opens.

The web server owner provides the API contract (`api-contract/`, kept out
of Git: get it privately from the project team). This repo
holds three small tools built from that contract:

- **`mock_server.py`**: a FastAPI mock of the API. Returns canned responses
  with the same shape as the real server, so the gate side can be tested
  before the real API key and IP whitelist are ready. No business logic.
- **`gate_client.py`**: a single-file CLI that runs on the Raspberry Pi and
  sends requests to either the mock or the real server. Python standard
  library only, so nothing needs to be installed on the Pi.
- **`gate_agent.py`**: the gate service. Reads the QR scanner, validates
  the scan with the server, opens the barrier through the `esp32c3-relay`
  driver, and reports back. See [Gate agent](#gate-agent).

## Folder structure

```
pi-gate-api-client/
├── api-contract/                    # from the web server owner, git-ignored
│   ├── pc-qr-api-guide.txt          # integration guide (Indonesian)
│   ├── pc-qr-openapi.json           # OpenAPI 3.0.3 spec
│   └── pc-qr-postman-collection.json
├── mock_server.py                   # FastAPI mock of the API
├── gate_client.py                   # API client + manual test CLI
├── gate_agent.py                    # scan -> validate -> open -> report
├── test_gate_agent.py               # self-check for gate_agent decisions
├── config.example.json              # template for config.json
├── requirements.txt                 # mock server dependencies only
└── README.md
```

`config.json`, `pc-api-*.json` (issued key files), `api-contract/` and the
agent's runtime `state/` are ignored by Git. Never commit them.

## Configuration

### Gate client

Copy the template and edit it:

```bash
cp config.example.json config.json
```

| Field         | Meaning                                                   |
|---------------|-----------------------------------------------------------|
| `base_url`    | Server root, without `/api/...`. Mock: `http://<host>:8000`. Real: from the issued key file |
| `estate_id`   | Estate ID in the URL path, from the issued key file      |
| `estate_code` | Estate code sent in gate reports, from the issued key file |
| `api_key`     | Sent as the `X-API-Key` header. Mock: must match `MOCK_API_KEY` |
| `timeout_s`   | HTTP timeout in seconds                                   |

For the real server, take `estate_id` and `api_key` from the
`pc-api-KODE.json` file issued by the web admin. The real server also checks
the source IP: requests must leave from the whitelisted public IP of the
monitoring room network, otherwise it answers `403 IP_NOT_ALLOWED`.

A different config file can be passed with `--config path/to/file.json`.

### Mock server

Configured with environment variables:

| Variable           | Default   |
|--------------------|-----------|
| `MOCK_API_KEY`     | `dev-key` |
| `MOCK_ESTATE_CODE` | `DEMO`    |

The mock does not check the source IP. The token in the scanned value picks
the `validate` result:

| Token contains        | Result                              |
|-----------------------|-------------------------------------|
| `reject`              | `rejected` / `qr_tidak_terdaftar`   |
| `pay` (with `--exit`) | `payment_required` / `tarif_parkir` |
| anything else         | `allowed`                           |

## Running the mock server

Requires Python 3.9+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
MOCK_API_KEY=dev-key uvicorn mock_server:app --host 0.0.0.0 --port 8000
```

Or with `uv`, no venv needed:

```bash
MOCK_API_KEY=dev-key uv run --with fastapi --with uvicorn \
  uvicorn mock_server:app --host 0.0.0.0 --port 8000
```

Use `--host 127.0.0.1` when the client runs on the same machine, and
`0.0.0.0` when another device (the Pi) must reach it. Interactive API docs
are at `http://<host>:8000/docs`.

## Running on the Raspberry Pi

Access path: local PC → Remote Desktop to the monitoring room PC → SSH to
the Pi.

1. From the monitoring room PC, open PowerShell and connect:

   ```bash
   ssh <user>@<pi-ip>
   ```

2. Get the code onto the Pi (first time), or update it:

   ```bash
   git clone <repo-url> ~/Project/pi-gate-api-client
   cd ~/Project/pi-gate-api-client
   git pull
   ```

3. Check Python (3.7+ for the client) and create the config:

   ```bash
   python3 --version
   cp config.example.json config.json
   nano config.json
   ```

4. Pick where the mock runs and set `base_url` to match:

   - **Mock on the Pi itself** (simplest, no network setup): open a second
     SSH session, start the mock there with `--host 127.0.0.1`, and use
     `"base_url": "http://127.0.0.1:8000"`. The Pi needs internet once to
     `pip install` the requirements.
   - **Mock on another machine on the same LAN**: start it with
     `--host 0.0.0.0` and use `"base_url": "http://<that-machine-ip>:8000"`.
     Check reachability from the Pi first:
     `curl http://<that-machine-ip>:8000/docs`. On Windows, allow port 8000
     in the firewall.
   - **Real server**: `base_url`, `estate_id` and `api_key` from the
     issued `pc-api-KODE.json`.

5. Send requests:

   ```bash
   python3 gate_client.py entries --upcoming
   python3 gate_client.py scan https://gate.example.com/verify/abc
   python3 gate_client.py validate pay-1 --exit
   python3 gate_client.py heartbeat
   ```

### Commands

| Command                                  | Endpoint                         |
|------------------------------------------|----------------------------------|
| `entries [--upcoming]`                   | `GET /entries`                   |
| `validate VALUE [--exit]`                | `POST /validate`                 |
| `scan VALUE [--exit]`                    | `POST /validate`, then `POST /events` if allowed |
| `event TOKEN [--exit] [--event-id ID] [--authorized-at TS]` | `POST /events` |
| `cancel EVENT_ID [--reason R]`           | `POST /cancel`                   |
| `batch FILE.json`                        | `POST /events/batch`             |
| `heartbeat [--entry-gate S] [--exit-gate S]` | `POST /heartbeat`            |
| `card UID [--exit] [--token TOKEN]`      | `POST /card-events`              |
| `capture FILE.jpg TOKEN [--exit]`        | `POST /vehicle-capture`          |

`VALUE` is the raw scanner output, either a `/verify/<token>` URL or a bare
token. `batch` takes a JSON file holding a list of gate reports (same body
as `/events`). Run `python3 gate_client.py <command> -h` for details.

## Verifying the client

There is no physical sign: `gate_client.py` never touches the relay, so the
barrier does not move. Success is shown in three places.

1. **Client output.** Each request prints `>>>` (what was sent) and `<<<`
   (HTTP status and JSON response). The exit code is `0` for HTTP 2xx and
   `1` otherwise, so `echo $?` after a command confirms it.

   ```
   >>> POST http://127.0.0.1:8000/api/pc-qr/1/validate
   <<< HTTP 200
   { "decision": "allowed", "reason": "sesuai_jadwal", ... }
   --- gate opened (simulated), sending report
   >>> POST http://127.0.0.1:8000/api/pc-qr/1/events
   <<< HTTP 200
   { "ok": true, "accepted": true, "duplicate": false, "reason": null }
   ```

2. **Mock server log.** Every request appears in the uvicorn terminal with
   the caller's IP. Seeing the Pi's IP there proves the Pi reached the mock:

   ```
   INFO:     192.168.1.50:51234 - "POST /api/pc-qr/1/validate HTTP/1.1" 200 OK
   ```

3. **Real server admin page.** Against the real server, open
   *Admin web > Validasi QR Gerbang*. After `heartbeat`, "Laporan kondisi
   terakhir" updates to the time just sent. After a `scan` or `event` on a
   real QR token, the delivery status changes.

### Common failures

| Output                              | Cause                                   |
|-------------------------------------|-----------------------------------------|
| `Config error (...)`                | `config.json` missing or invalid JSON   |
| `Connection failed: Connection refused` | Mock not running, wrong host/port, or bound to `127.0.0.1` while called from another device |
| `Connection failed: timed out`      | Firewall or no route to `base_url`      |
| `HTTP 401` `INVALID_API_KEY`        | `api_key` does not match the server (or key was regenerated) |
| `HTTP 403` `IP_NOT_ALLOWED`         | Real server: request is not from the whitelisted public IP |
| `HTTP 404` `ESTATE_NOT_FOUND`       | Wrong `estate_id`                       |
| `clock_ok: false` in heartbeat      | Pi clock is off by more than 60 s. Check NTP: `timedatectl` |

## Gate agent

`gate_agent.py` is the service that runs the gate unattended.

### Flow

```
scanner ──scan──> gate_agent
                      │
          token already used locally? ──yes──> DENY used_locally
                      │
          entry and QR in cached list? ──yes──> slot check (local only)
                      │ no (or exit)            outside slot ─> DENY slot_too_*
                      │                         inside slot ──> allowed
          POST /validate to server                    │
          network error / 5xx? ──yes──> offline decision (see below)
                      │                               │
          decision != allowed ──────────────> DENY <reason>
                      │ allowed  <────────────────────┘
          mark token used (state/used_tokens.json)
                      │
          pulse relay ── failed ──> unmark, POST /cancel (server permit only),
                      │                DENY gate_failed
                      │   unknown ─> keep token used, DENY gate_unknown
                      │ opened
          POST /events (queued to state/pending_reports.jsonl on failure)
                      │
                   OPEN
```

An entry QR in the cached list never waits for the server: the barrier
opens first and the report is sent after. The server is asked only for a
QR not in the list (e.g. registered after the last sync) and for exits.

| Result code          | Meaning                                                 |
|----------------------|---------------------------------------------------------|
| `sesuai_jadwal`, `boleh_keluar` | Barrier opened, report sent or queued        |
| `used_locally`       | Token already opened the barrier on this Pi (guide section 2 requires this check) |
| server reasons       | e.g. `qr_tidak_terdaftar`, `qr_sudah_masuk`, `di_luar_jadwal`: barrier stays shut |
| `payment_required`   | Late exit, payment needed. Not handled yet, barrier stays shut |
| `api_error_<status>` | 401/403/404: wrong key, IP or estate ID in `config.json` |
| `gate_failed`        | Relay surely did not fire (ESP32 offline or refused). Permit cancelled, token can be scanned again |
| `gate_unknown`       | Relay accepted the pulse but never confirmed. Token kept as used, no report sent |
| `local_slot`         | Entry opened from the cached list without asking the server |
| `offline_exit`       | Server down, exit opened because this Pi recorded the entry |
| `offline_not_in_cache` | Server down, token not in the last synced list (e.g. registered after the last sync) |
| `slot_too_early`, `slot_too_late` | Token cached but outside its slot, server not asked |
| `offline_cache_expired` | Server down, no list synced yet or past `cache_valid_until` |
| `offline_not_entered` | Server down, exit for a token this Pi never let in      |

Each `used_locally` record is per direction, so the same QR can enter once
and exit once.

### Cache and offline fallback

This job runs once at startup, before the agent accepts any scan, so the
cache is fresh from the first scan. After that a background thread repeats
it every `sync_interval_s` (default 1 minute, so a newly registered QR
reaches the cache quickly):

1. `GET /entries?include_upcoming=1`: QR codes whose slot is running or
   starts within 6 hours, each with `valid_from` and `valid_until`. Saved to
   `state/qr_cache.json`, so it survives a reboot. A failed pull keeps the
   previous list.
2. If the pull worked, resend queued reports with `/events/batch`, reusing
   the same `event_id` so the server counts each once.

Entry scans are decided from the cache first (see Flow). With the
server asked for a QR not in the list and that call failing (network
error or HTTP 5xx), the offline rules apply (guide "SKENARIO GANGGUAN").
A `rejected` answer from the server is final.

- **Entry**: allowed only if the token is in the last synced list,
  `valid_from <= Pi time < valid_until`, and the list is not past
  `cache_valid_until`. This is the same check for online and offline,
  so a QR registered after the last sync needs the server.
- **Exit**: allowed only if this Pi recorded the token's entry. The server
  books any late fee as outstanding when the report arrives.
- Cache and offline opens are reported with a new `event_id` and the Pi's time as
  `authorized_at`, queued until the server is back.

Cache decisions depend on the Pi clock. The Pi has no battery clock, so
check NTP sync with `timedatectl` (`System clock synchronized: yes`).

### Configuration

The agent reads the `agent` section of the same `config.json`:

| Field              | Meaning                                                 |
|--------------------|---------------------------------------------------------|
| `relay_port`       | ESP32-C3 serial port, e.g. `/dev/ttyACM0` (a `/dev/serial/by-id/...` path survives re-plugging) |
| `relay_driver_dir` | Folder holding `test_relay.py` from `esp32c3-relay/pi`  |
| `state_dir`        | Runtime state, relative to this folder (default `state`) |
| `sync_interval_s`  | Cache pull and report retry interval (default 60)       |
| `used_token_days`  | Days a used token is remembered (default 7). Also how long after entry an offline exit still works |
| `gates[]`          | One entry per scanner/barrier pair                      |
| `gates[].direction`| `entry` or `exit`                                       |
| `gates[].scanner`  | Scanner input device. Find it with `ls /dev/input/by-id/` |
| `gates[].relay`    | Relay number on the ESP32-C3 (1 = GPIO6, 2 = GPIO7)     |
| `gates[].pulse_ms` | Relay pulse length, 1 to 5000 ms                        |

### Running on the Pi

Both repos are expected side by side, e.g. `~/Project/esp32c3-relay` and
`~/Project/pi-gate-api-client`.

1. Install the two hardware libraries (the API part needs nothing):

   ```bash
   sudo apt install python3-evdev python3-serial
   ```

   Or inside the existing `esp32c3-relay/.venv`: `pip install evdev`
   (pyserial is already there).

2. Allow the user to read the scanner and the serial port without sudo,
   then log out and back in:

   ```bash
   sudo usermod -aG input,dialout $USER
   ```

3. Run the self-check, then go step by step:

   ```bash
   python3 test_gate_agent.py
   python3 gate_agent.py --stdin --dry-run    # type scans, no hardware
   python3 gate_agent.py --dry-run            # real scanner, no relay
   python3 gate_agent.py                      # full: scanner + relay
   ```

   Add `-v` to print every API request and response. Stop with Ctrl+C.

### Verifying the agent

Test with the mock first (`base_url` pointing at it), then with the real
server.

1. **`--stdin --dry-run`**: type `abc` and press Enter. The log shows
   `[dry-run] would pulse relay 1` then `OPEN: sesuai_jadwal`. Type `abc`
   again: `DENY: used_locally`. Type `reject-1`: `DENY: qr_tidak_terdaftar`.
   The mock log shows `/validate` and `/events` calls.
2. **`--dry-run` with the scanner**: the log shows `scanner ready`. Scan a
   QR code: one `OPEN` or `DENY` line per scan. Nothing is typed into the
   Pi console (the agent grabs the scanner).
3. **Full run**: scan an allowed QR code.
   - **Physical**: the relay clicks, the relay module LED (if fitted) lights for
     `pulse_ms`, and the barrier opens.
   - **Log**: `OPEN: sesuai_jadwal`, with no `queued for retry` warning.
   - **Server**: on the real server the delivery moves to "inside" in
     *Admin web > Validasi QR Gerbang*.

   Scan a rejected QR code: no click, `DENY` with the reason.
   The log also shows `cache synced: N entries` at startup and every
   `sync_interval_s`.
4. **Offline fallback** (mock serves one cached token, `contoh-token-qr`):
   start the agent and wait for `cache synced`, then stop the mock.
   - Scan `contoh-token-qr`: `OPEN: local_slot` plus `report ... queued
     for retry`.
   - Scan any other token: `DENY: offline_not_in_cache`.
   - Start the mock again. At the next sync the log shows `flushed 1 queued
     reports, 0 left` and the mock logs `POST .../events/batch`.

   On site, pull the Pi's network cable instead of stopping the mock.
5. **Failure checks**: unplug the ESP32-C3 and scan, which should give
   `DENY: gate_failed`.

To start over during testing, stop the agent and delete `state/`.

### Running as a service

`gate-agent.service` starts the agent on boot and restarts it 5 s after
any crash, forever.

1. Find the three values for `gate-agent.service`. Log in to the Pi over
   SSH as the user that normally runs the agent (the one that ran
   `python3 gate_agent.py` successfully), then go to this repo's folder:

   ```bash
   cd ~/Project/pi-gate-api-client   # or wherever this repo was cloned
   whoami                            # -> value for User
   pwd                               # -> value for WorkingDirectory
   which python3                     # -> Python path for ExecStart
   ```

   Example output, for user `pi`:

   ```
   pi
   /home/pi/Project/pi-gate-api-client
   /usr/bin/python3
   ```

   If you do not know where the repo is, search for it:
   `find ~ -name gate_agent.py`. The folder that holds the file is the
   `WorkingDirectory`.

   Also check that this user has the `input` and `dialout` groups (step 2
   above). Both names must appear in the output of:

   ```bash
   groups
   ```

   If you ran the agent with a virtualenv activated (the prompt starts
   with `(.venv)`), `which python3` prints the venv's Python, for example
   `/home/pi/Project/esp32c3-relay/.venv/bin/python3`. Use that path, since
   the libraries are installed there. Otherwise it prints
   `/usr/bin/python3`.

2. Put the values into `gate-agent.service`. `ExecStart` is the Python
   path, a space, then the full path to `gate_agent.py` (the
   `WorkingDirectory` plus `/gate_agent.py`). With the example output
   above:

   ```ini
   User=pi
   WorkingDirectory=/home/pi/Project/pi-gate-api-client
   ExecStart=/usr/bin/python3 /home/pi/Project/pi-gate-api-client/gate_agent.py
   ```

   Edit the file on the Pi with `nano gate-agent.service`, save with
   Ctrl+O then Enter, and exit with Ctrl+X. Do not use `~` in these
   lines: systemd does not expand it, so write the full `/home/...` path.

   Before installing, test the exact `ExecStart` line by hand. It must
   start without errors (stop it with Ctrl+C):

   ```bash
   /usr/bin/python3 /home/pi/Project/pi-gate-api-client/gate_agent.py --dry-run
   ```

3. Install and start it:

   ```bash
   sudo cp gate-agent.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now gate-agent
   ```

4. Keep logs across reboots, capped at 50 MB so the SD card is not filled:

   ```bash
   sudo mkdir -p /etc/systemd/journald.conf.d
   printf '[Journal]\nStorage=persistent\nSystemMaxUse=50M\n' \
     | sudo tee /etc/systemd/journald.conf.d/gate.conf
   sudo systemctl restart systemd-journald
   ```

Daily use:

| Task                          | Command                                         |
|-------------------------------|-------------------------------------------------|
| Status and last log lines     | `systemctl status gate-agent`                   |
| Follow the log live           | `journalctl -u gate-agent -f`                   |
| Log of one period             | `journalctl -u gate-agent --since "2026-11-12 14:00" --until "2026-11-12 16:00"` |
| Problems only                 | `journalctl -u gate-agent -p warning`           |
| Find one QR or delivery       | `journalctl -u gate-agent \| grep KITB-20261112-DD6F3D` |
| Restart after editing config  | `sudo systemctl restart gate-agent`             |
| Stop for manual testing       | `sudo systemctl stop gate-agent`                |

Each scan logs one line, for example
`[entry] OPEN: sesuai_jadwal qr=Xk2D8nF0 KITB-20261112-DD6F3D`. `qr=` is
the first 8 characters of the token. The `KITB-...` tracking code is the
one shown on the web dashboard; the server sends it only for allowed and
payment-required scans, so rejected scans are matched by `qr=` and time.

State files are written with fsync. If a power cut still leaves one
unreadable, the agent logs `unreadable`, moves it to `*.corrupt` and
starts with an empty one instead of crashing.

### Extending

Each concern lives in one place in `gate_agent.py`:

| Requirement                         | Where                                        |
|-------------------------------------|----------------------------------------------|
| Different offline rules             | `GateAgent.validate_offline()`, `QrCache.check()` |
| Buzzer, LED, display feedback       | `GateAgent.on_result()`                       |
| Exit payment (`payment_required`)   | `GateAgent._handle()`, after `validate()`     |
| Heartbeat every minute (guide section 6) | new thread in `main()` calling `/heartbeat` with relay health |
| Another barrier driver              | new class with `open(gate)` returning `opened`/`failed`/`unknown` |
| Another scanner type                | new generator like `evdev_scans()` yielding scan strings |

Add a case to `test_gate_agent.py` for each new decision path.
