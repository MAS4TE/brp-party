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

1. After each market clearing, ASSUME sends the accepted orders, including the
   counterparties and their blockchain addresses, to `POST /market_results`. The service
   stores them, so it knows who was awarded how much and with whom.
2. A buyer sends its schedule with its blockchain address to `POST /power_request`. The
   service validates and aggregates it, assigns it to the sellers the buyer was matched with
   (share by matched volume; rejected if the buyer has no awarded storage for the period),
   persists it to SQLite, and returns the `request_id` it assigned.
3. The sellers poll `GET /power_requests` (e.g. every 30 s) with their blockchain address and
   get their share of every schedule they still have to fulfil.
4. A seller drives its share and reports the actually driven schedule back to
   `POST /actual_schedule`. The service validates it against its share of the stored request
   and returns the validation result synchronously.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Optional: create a `.env` file in the repository root (loaded via `python-dotenv`).

## Run

```bash
export API_PASSWORD=secret
export DB_PATH=database.db

uvicorn --app-dir ./api main:app --reload
```

Interactive API docs: `http://localhost:8000/docs`

See [api/README.md](api/README.md) for endpoint details.
