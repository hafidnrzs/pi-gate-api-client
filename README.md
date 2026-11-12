# pi-gate-api-client

Test tooling for integrating an estate gate (Raspberry Pi) with the QR
validation web server run by the estate web owner.

The web server owner provides the API contract (`api-contract/`, kept out
of Git: get it privately from the project team). This repo
holds two small tools built from that contract:

- **`mock_server.py`**: a FastAPI mock of the API. Returns canned responses
  with the same shape as the real server, so the gate side can be tested
  before the real API key and IP whitelist are ready. No business logic.
- **`gate_client.py`**: a single-file CLI that runs on the Raspberry Pi and
  sends requests to either the mock or the real server. Python standard
  library only, so nothing needs to be installed on the Pi.

The client does **not** drive the gate relay. It only exercises the HTTP
API. Opening the barrier is handled separately by `esp32c3-relay`.

## Folder structure

```
pi-gate-api-client/
├── api-contract/                    # from the web server owner, git-ignored
│   ├── pc-qr-api-guide.txt          # integration guide (Indonesian)
│   ├── pc-qr-openapi.json           # OpenAPI 3.0.3 spec
│   └── pc-qr-postman-collection.json
├── mock_server.py                   # FastAPI mock of the API
├── gate_client.py                   # CLI for the Raspberry Pi
├── config.example.json              # template for config.json
├── requirements.txt                 # mock server dependencies only
└── README.md
```

`config.json`, `pc-api-*.json` (issued key files) and `api-contract/` are
ignored by Git. Never commit them.

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

## Verifying it works

There is no physical sign: the client never touches the relay, so the
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
