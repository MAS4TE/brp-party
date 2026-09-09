<!--
SPDX-FileCopyrightText: MAS4TE Developers

SPDX-License-Identifier: MIT
-->

# brp-party

Single FastAPI service for BRP schedule handling.

## Layout

```
./api/        # the service (entry: main.py)
  main.py     #   HTTP endpoints
  models.py   #   request/response schemas
  util.py     #   aggregation, validation, persistence
```

## Flow

1. Lina sends a power-request schedule to `POST /power_request`. The service validates
   and aggregates it, persists it to SQLite, publishes it to Bob via MQTT, and returns the
   `request_id` it assigned.
2. Bob drives the schedule and reports the actually driven schedule back to
   `POST /actual_schedule`. The service validates it against the stored request and
   returns the validation result synchronously (no MQTT round-trip).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Optional: create a `.env` file in the repository root (loaded via `python-dotenv`).

## Run

```bash
export API_PASSWORD=secret
export MQTT_HOST=your-broker.example.com
export MQTT_PORT=8883
export MQTT_USER=your-user
export MQTT_PASSWORD=your-password
export REQUESTING_SCHEDULE_TOPIC=mas4te/battery-storage/power_request
export DB_PATH=database.db

uvicorn --app-dir ./api main:app --reload
```

Interactive API docs: `http://localhost:8000/docs`

See [api/README.md](api/README.md) for endpoint details.
