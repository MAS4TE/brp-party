<!--
SPDX-FileCopyrightText: MAS4TE Developers

SPDX-License-Identifier: MIT
-->

# api

FastAPI service under `./api/` with two endpoints:

- `POST /power_request` — accepts up to four timeseries, validates them, sums them
  element-wise, persists the aggregated result to SQLite, and publishes it to MQTT over TLS.
- `POST /actual_schedule` — accepts the schedule actually driven by the fulfiller,
  validates it against the stored request, persists it, and returns the validation result.

## Installation

From the repository root:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Optional: create a `.env` file in the repository root (loaded via `python-dotenv`).

## Configuration (environment variables)

| Variable | Required | Purpose |
| -------- | -------- | ------- |
| `API_PASSWORD` | yes | Password for HTTP Basic Auth |
| `MQTT_HOST` | yes | MQTT broker hostname |
| `MQTT_PORT` | yes | MQTT broker port (TLS) |
| `REQUESTING_SCHEDULE_TOPIC` | yes | Topic for the aggregated power request |
| `MQTT_USER` | no | MQTT username |
| `MQTT_PASSWORD` | no | MQTT password |
| `DB_PATH` | yes | SQLite database file |

## Running

From the repository root:

```bash
export API_PASSWORD=secret
export MQTT_HOST=your-broker.example.com
export MQTT_PORT=8883
export REQUESTING_SCHEDULE_TOPIC=mas4te/battery-storage/power_request
export MQTT_USER=your-user
export MQTT_PASSWORD=your-password
export DB_PATH=database.db

uvicorn --app-dir ./api main:app --reload
```

Interactive API docs: `http://localhost:8000/docs`

## Endpoint: `POST /power_request`

- **Authentication:** HTTP Basic Auth (username is ignored; password must match `API_PASSWORD`).
- **Body:** 1–4 optional series (`eeg`, `wholesale`, `community`, `home`) plus `time_steps`.
- **Query parameter:** `is_real_world_test` (boolean, default `false`). When `true`, additional checks apply (see below).
- **Response:** `200 OK` with the `request_id` assigned to this request.

### Request body schema

```json
{
  "time_steps": [
    "2026-05-08T12:00:00",
    "2026-05-08T12:15:00"
  ],
  "eeg": [0.0, 0.0],
  "wholesale": [0.02, 0.98],
  "community": [0.0, 0.0],
  "home": [0.0, 0.0]
}
```

At least one series must be present. Each provided series must have the same length as `time_steps`.

### Response

```json
{
  "request_id": "200312c0-cb21-479d-82ec-037077f0bc44"
}
```

The same `request_id` is published to the fulfiller via MQTT and is the key the fulfiller
reports back to `POST /actual_schedule`. Keep it if you want to correlate a request with the
schedule that was actually driven.

**Validation (always):**

- `time_steps` must be ISO 8601 datetimes, strictly increasing, with no duplicates.
- Do not mix timezone-aware and timezone-naive values in one request.
- Series lengths must match `time_steps` (enforced by Pydantic and again in the handler).

**Validation (`is_real_world_test=true` only):**

- The first `time_step` must align to a quarter-hour (`:00`, `:15`, `:30`, `:45`, seconds and microseconds zero).
- If there is more than one step, the first step must be strictly before the last.
- The first step must be more than 15 minutes in the future (relative to server time, using the request timezone when present).

### SQLite persistence

On success, each timestep is stored as one row in the table `requested_aggregated_schedules`
in the database at `DB_PATH` (long table: `request_id`, `time_steps`, `power_rate_w`,
`requested_at` — UTC timestamp shared by all rows of the request).
Rows are written and committed before the MQTT publish. If the insert itself fails, the
transaction is rolled back and nothing is stored. If the insert succeeds but the subsequent
MQTT publish fails, the rows stay in the database and the request still returns `500` — the
stored request and what was actually published to Bob can therefore diverge.

### MQTT payload

On success, the service publishes a JSON object with `request_id`, `time_steps`, and `power_rate_w` (aggregated power in watts):

```json
{
  "request_id": "…",
  "time_steps": ["2026-05-08 12:00:00", "2026-05-08 12:15:00"],
  "power_rate_w": [20.0, 980.0]
}
```

Publishing uses QoS 1 and TLS (`tls={}` in the Paho client).

### Error responses

| Status | When |
| ------ | ---- |
| `401` | Invalid HTTP Basic password |
| `422` | Validation failed (body or `is_real_world_test` rules) |
| `500` | `API_PASSWORD` not configured, SQLite persist failed, or MQTT publish failed |

## Example: build a request with pandas

```python
import pandas as pd
import requests

timestamps = pd.date_range("2026-05-08T12:00:00", periods=4, freq="15min")
df = pd.DataFrame(
    {
        "eeg": [0.0, 0.1, 0.2, 0.0],
        "wholesale": [0.5, 0.5, 0.5, 0.5],
        "community": [0.0, 0.0, 0.0, 0.0],
        "home": [0.0, 0.0, 0.0, 0.0],
    },
    index=timestamps,
)

payload = {
    "time_steps": [ts.isoformat() for ts in df.index],
    **{col: df[col].tolist() for col in df.columns},
}

response = requests.post(
    "http://localhost:8000/power_request",
    params={"is_real_world_test": False},
    auth=("brp", "secret"),
    json=payload,
)
response.raise_for_status()
request_id = response.json()["request_id"]
```

### curl example

```bash
curl -u brp:secret \
  -X POST "http://localhost:8000/power_request" \
  -H "Content-Type: application/json" \
  -d '{
    "time_steps": ["2026-05-08T12:00:00", "2026-05-08T12:15:00"],
    "eeg": [0.0, 0.0],
    "wholesale": [0.02, 0.98]
  }'
```

Real-world test mode:

```bash
curl -u brp:secret \
  -X POST "http://localhost:8000/power_request?is_real_world_test=true" \
  -H "Content-Type: application/json" \
  -d @payload.json
```

## Endpoint: `POST /actual_schedule`

Reported by the fulfiller after driving a schedule. The service validates the actually
driven schedule against the original request (matched by `request_id`), stores it, and
returns the validation result synchronously.

- **Authentication:** HTTP Basic Auth (username is ignored; password must match `API_PASSWORD`).
- **Body:** `request_id`, `time_steps`, and `power_rate_w` (equal lengths).
- **Response:** `200 OK` with the validation result as JSON.

### Request body schema

```json
{
  "request_id": "200312c0-cb21-479d-82ec-037077f0bc44",
  "time_steps": ["2026-05-08 12:00:00", "2026-05-08 12:15:00"],
  "power_rate_w": [20.0, 980.0]
}
```

`request_id` must be the one returned by (and published for) `POST /power_request`, and
`time_steps` must equal the stored ones.

### Response

On success:

```json
{
  "request_id": "200312c0-cb21-479d-82ec-037077f0bc44",
  "valid": true,
  "errors": [],
  "missing_energy_wh": 0.0
}
```

On business-validation failure (unknown `request_id`, `time_steps` mismatch) — still `200 OK`:

```json
{
  "request_id": "200312c0-cb21-479d-82ec-037077f0bc44",
  "valid": false,
  "errors": ["time_steps mismatch: expected [...], got [...]."]
}
```

`missing_energy_wh` is the missing discharge energy (Wh) summed over timesteps where the
requested power is negative and the actual power is below requested
(see `calc_missing_energy_timeseries` in `util.py`).

### SQLite persistence

What gets stored in the table `actual_schedules` at `DB_PATH` depends on the validation
outcome:

- **Valid** (`time_steps` match the stored request): one row per timestep, with
  `time_steps`, `power_rate_w` (actual), and the per-timestep `missing_energy_wh`.
- **Invalid** (unknown `request_id`, `time_steps` mismatch): a single summary row with
  `time_steps`/`power_rate_w`/`missing_energy_wh` set to `NULL` and `validation_errors` filled in.

All rows have `request_id`, `received_at` (UTC timestamp of this submission), and `valid`.

### Error responses

| Status | When |
| ------ | ---- |
| `401` | Invalid HTTP Basic password |
| `422` | Body does not match the schema (e.g. `power_rate_w`/`time_steps` length mismatch) |
| `500` | SQLite read/persist failed |

### curl example

```bash
curl -u bob:secret \
  -X POST "http://localhost:8000/actual_schedule" \
  -H "Content-Type: application/json" \
  -d '{
    "request_id": "200312c0-cb21-479d-82ec-037077f0bc44",
    "time_steps": ["2026-05-08 12:00:00", "2026-05-08 12:15:00"],
    "power_rate_w": [20.0, 980.0]
  }'
```
