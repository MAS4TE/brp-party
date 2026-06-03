<!--
SPDX-FileCopyrightText: MAS4TE Developers

SPDX-License-Identifier: MIT
-->

# schedule-validation

Long-running MQTT service under `./src/schedule-validation/` that validates fulfillment
schedules against the originals stored by [schedule-api](../schedule-api/).

## Flow

1. An external fulfiller publishes a schedule to `ACTUAL_SCHEDULES_TOPIC`.
2. This service loads the original schedule from SQLite (`requested_aggregated_schedules`)
   by `request_id`.
3. It compares `time_steps` between actual and requested schedule.
4. On match, it stores the received schedule and computes missing discharge energy (Wh).
5. A JSON validation response is published back to the same `ACTUAL_SCHEDULES_TOPIC`.

## Configuration (environment variables)

| Variable | Required | Purpose |
| -------- | -------- | ------- |
| `MQTT_HOST` | yes | MQTT broker hostname |
| `MQTT_PORT` | yes | MQTT broker port (TLS) |
| `ACTUAL_SCHEDULES_TOPIC` | yes | Topic for incoming schedules and validation responses |
| `MQTT_USER` | no | MQTT username |
| `MQTT_PASSWORD` | no | MQTT password |
| `DB_PATH` | yes | SQLite database (same file as schedule-api) |

Optional: create a `.env` file in the repository root (loaded via `python-dotenv`).

## Running

From the repository root:

```bash
export MQTT_HOST=your-broker.example.com
export MQTT_PORT=8883
export ACTUAL_SCHEDULES_TOPIC=mas4te/battery-storage/actual_schedules
export MQTT_USER=your-user
export MQTT_PASSWORD=your-password
export DB_PATH=database.db

python ./src/schedule-validation/main.py
```

## Payload formats

### Incoming (actual schedules topic)

Same shape as the schedule-api MQTT payload:

```json
{
  "request_id": "200312c0-cb21-479d-82ec-037077f0bc44",
  "time_steps": ["2026-05-08 12:00:00", "2026-05-08 12:15:00"],
  "power_rate_w": [20.0, 980.0]
}
```

### Outgoing (same topic)

On failure:

```json
{
  "request_id": "200312c0-cb21-479d-82ec-037077f0bc44",
  "valid": false,
  "errors": ["time_steps mismatch: expected [...], got [...]."]
}
```

On success:

```json
{
  "request_id": "200312c0-cb21-479d-82ec-037077f0bc44",
  "valid": true,
  "errors": [],
  "missing_energy_wh": 0.0
}
```

## Validation rules

- Payload must be valid JSON and match the `request_id` / `time_steps` / `power_rate_w` schema.
- `request_id` must exist in `requested_aggregated_schedules` with matching `time_steps`.
- Missing discharge energy is summed over timesteps where requested power is negative
  and actual power is below requested (see `calc_schedule_differences` in `util.py`).
- Invalid JSON or schema errors produce `valid: false` with a descriptive `errors` list.

## Database

Reads from `requested_aggregated_schedules` (written by schedule-api).

On startup, creates `actual_schedules` if missing (columns: `request_id`, `time_steps`,
`power_rate_w`, `received_at`, `valid`, `validation_errors`).
