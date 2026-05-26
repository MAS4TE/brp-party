<!--
SPDX-FileCopyrightText: MAS4TE Developers

SPDX-License-Identifier: MIT
-->

# brp-api

Compact FastAPI service that accepts up to four timeseries, validates them,
sums them element-wise, and publishes the aggregated result to MQTT over TLS.

## Installation

```bash
cd brp-api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Optional: create a `.env` file in `brp-api/` (loaded via `python-dotenv` if installed).

## Configuration (environment variables)

| Variable         | Required | Purpose                                      |
| ---------------- | -------- | -------------------------------------------- |
| `API_PASSWORD`   | yes      | Password for HTTP Basic Auth                 |
| `MQTT_HOST`      | yes      | MQTT broker hostname                         |
| `MQTT_PORT`      | yes      | MQTT broker port (TLS)                       |
| `MQTT_TOPIC`     | yes      | Topic for the aggregated power request       |
| `MQTT_USER`      | no       | MQTT username                                |
| `MQTT_PASSWORD`  | no       | MQTT password                                |

## Running

```bash
export API_PASSWORD=secret
export MQTT_HOST=your-broker.example.com
export MQTT_PORT=8883
export MQTT_TOPIC=mas4te/battery-storage/power_request
export MQTT_USER=your-user
export MQTT_PASSWORD=your-password

uvicorn main:app --reload
```

Interactive API docs: `http://localhost:8000/docs`

## Endpoint

`POST /power_request`

- **Authentication:** HTTP Basic Auth (username is ignored; password must match `API_PASSWORD`).
- **Body:** 1–4 optional series (`eeg`, `wholesale`, `community`, `home`) plus `time_steps`.
- **Query parameter:** `is_real_world_test` (boolean, default `false`). When `true`, additional checks apply (see below).
- **Response:** `200 OK` with an empty body on success.

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

**Validation (always):**

- `time_steps` must be ISO 8601 datetimes, strictly increasing, with no duplicates.
- Do not mix timezone-aware and timezone-naive values in one request.
- Series lengths must match `time_steps` (enforced by Pydantic and again in the handler).

**Validation (`is_real_world_test=true` only):**

- The first `time_step` must align to a quarter-hour (`:00`, `:15`, `:30`, `:45`, seconds and microseconds zero).
- If there is more than one step, the first step must be strictly before the last.
- The first step must be more than 15 minutes in the future (relative to server time, using the request timezone when present).

### MQTT payload

On success, the service publishes a JSON object mapping each timestep (string) to the summed value across all provided series:

```json
{
  "2026-05-08 12:00:00": 0.02,
  "2026-05-08 12:15:00": 0.98
}
```

Publishing uses QoS 1 and TLS (`tls={}` in the Paho client).

### Error responses

| Status | When |
| ------ | ---- |
| `401`  | Invalid HTTP Basic password |
| `422`  | Validation failed (body or `is_real_world_test` rules) |
| `500`  | `API_PASSWORD` not configured, or MQTT publish failed |

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

requests.post(
    "http://localhost:8000/power_request",
    params={"is_real_world_test": False},
    auth=("brp", "secret"),
    json=payload,
).raise_for_status()
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
