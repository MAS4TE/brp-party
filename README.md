<!--
SPDX-FileCopyrightText: MAS4TE Developers

SPDX-License-Identifier: MIT
-->

# brp-party

Monorepo for BRP schedule handling.

## Layout

```
./src/
  power_request_api/     # HTTP → aggregate → MQTT (entry: main.py)
  fulfilment_validation/ # MQTT fulfillment validation (entry: main.py)
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run power_request_api

```bash
export API_PASSWORD=secret
export MQTT_HOST=your-broker.example.com
export MQTT_PORT=8883
export REQUESTING_SCHEDULE_TOPIC=mas4te/battery-storage/power_request
export DB_PATH=database.db

uvicorn --app-dir ./src/power_request_api main:app --reload
```

See [src/power_request_api/README.md](src/power_request_api/README.md) for API details.

## Run fulfilment_validation

```bash
export MQTT_HOST=your-broker.example.com
export MQTT_PORT=8883
export ACTUAL_SCHEDULES_TOPIC=mas4te/battery-storage/actual_schedules
export DB_PATH=database.db

python ./src/fulfilment_validation/main.py
```

See [src/fulfilment_validation/README.md](src/fulfilment_validation/README.md) for details.
