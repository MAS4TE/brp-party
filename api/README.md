<!--
SPDX-FileCopyrightText: MAS4TE Developers

SPDX-License-Identifier: MIT
-->

# api

FastAPI service under `./api/` with four endpoints:

- `POST /power_request` — accepts up to four timeseries of a buyer, validates them, sums
  them element-wise, assigns the result to the sellers the buyer was matched with in the
  market, and persists it to SQLite.
- `GET /power_request` — polled by the sellers: their share of every schedule they still
  have to fulfil.
- `POST /actual_schedule` — accepts the schedule actually driven by a seller,
  validates it against its share of the stored request, persists it, and returns the
  validation result.
- `POST /market_results` — accepts the accepted orders of a market clearing from ASSUME
  and persists them, including the matched counterparties.

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
| `POWER_REQUEST_PASSWORD` | yes | Password for HTTP Basic Auth of buyers and sellers |
| `MARKET_RESULTS_PASSWORD` | yes | HTTP Basic Auth password of ASSUME (username `assume`) at `POST /market_results` |
| `DB_PATH` | yes | SQLite database file |

[`example.env`](../example.env) lists them; copy it to `.env` in the repository root.

## Running

From the repository root:

```bash
export POWER_REQUEST_PASSWORD=secret
export MARKET_RESULTS_PASSWORD=other-secret
export DB_PATH=database.db

uvicorn --app-dir ./api main:app --reload
```

Interactive API docs: `http://localhost:8000/docs`

## Endpoint: `POST /power_request`

- **Authentication:** HTTP Basic Auth (username is ignored; password must match `POWER_REQUEST_PASSWORD`).
- **Body:** the buyer's `blockchain_address`, the `trade_ids` the schedule is made up of, 1–4 optional series (`eeg`, `wholesale`,
  `community`, `home`) plus `time_steps`.
- **Query parameter:** `is_real_world_test` (boolean, default `false`). When `true`, additional checks apply (see below).
- **Response:** `200 OK` with the `request_id` assigned to this request.

### Request body schema

```json
{
  "blockchain_address": "0x...",
  "trade_ids": [
    "0b6f5c1e-8a4d-4e7b-9f3a-2c1d5e6a7b80",
    "7d2e9a14-3b5c-4f60-8a1e-9c4b2d7f1a35"
  ],
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

The same `request_id` is handed out to the assigned sellers by `GET /power_request` and is
the key they report back to `POST /actual_schedule`. Keep it if you want to correlate a
request with the schedule that was actually driven.

**Validation (always):**

- `time_steps` must be ISO 8601 datetimes, strictly increasing, with no duplicates.
  Ordering is checked on the parsed datetimes, so it holds across differing UTC offsets.
- Consecutive `time_steps` must be exactly 15 minutes apart.
- At least one series must be present.
- Series values must be finite numbers (`NaN` and `Infinity` are rejected).
- Do not mix timezone-aware and timezone-naive values in one request.
- Series lengths must match `time_steps` (enforced by Pydantic and again in the handler).

**Validation (`is_real_world_test=true` only):**

- The first `time_step` must align to a quarter-hour (`:00`, `:15`, `:30`, `:45`, seconds and microseconds zero).
- If there is more than one step, the first step must be strictly before the last.
- The first step must be more than 15 minutes in the future (relative to server time, using the request timezone when present).

### Assignment to the sellers

The schedule is assigned from the market results stored by `POST /market_results`:

- `trade_ids` lists the trades of the buyer the schedule is made up of (the `trade_id`s in
  the `matches` of its market result). The buyer does not need to know the sellers, and a
  schedule may span several trades, also with the same seller. At least one is required, no
  duplicates. A trade may be used by several requests, e.g. for different parts of its
  delivery period.
- Each trade must be a trade of the buyer (`blockchain_address` = the buyer's, negative
  `accepted_volume`) whose delivery period covers the whole schedule, i.e. `start_time` ≤ first
  `time_step` and last `time_step` + 15 min ≤ `end_time`. Otherwise the request is rejected
  with `422` naming the offending trades, and nothing is stored.
- Every seller of these trades gets a share of the schedule proportional to the matched
  volume of **these** trades (e.g. 1 kWh and 3 kWh → 25 % and 75 %). Other trades of the
  buyer are not considered.

### Storage limits

A schedule must not store more or faster than the trades it is made up of allow. The
storage is empty at the start of the schedule (positive power charges, negative power
discharges). With `volume` the matched volume (kWh) and `c_rate` of the trade's order
(1 kWh at c-rate 1 = 1 kW), summed over the trades:

- **Power:** in no time step may the schedule charge or discharge faster than
  `Σ volume × c_rate` (kW).
- **Energy:** the stored energy (cumulative power × 15 min) may not fall below empty and
  may not exceed `Σ volume` kWh in any time step.
- **End:** the storage has to be empty again at the end of the schedule, i.e. the schedule
  discharges exactly what it charged.

Charging losses are not considered. Otherwise the request is rejected with `422` and
nothing is stored. The `actual_schedule` of each seller is checked against the power and
energy limits of its own trades (see below); it does not have to end empty, a shortfall
shows up as missing energy. Limits are not checked across different requests of the same
buyer, each starts and ends empty.

### Replacing an earlier request

A new request replaces the buyer's earlier requests (same `blockchain_address`) where they
overlap: their `time_steps` that the new request also covers are deleted, the other
`time_steps` stay. A request that loses all its `time_steps` this way is no longer handed out
by `GET /power_request`. With `is_real_world_test=true`, the checks above run first, so only
`time_steps` more than 15 minutes in the future can be replaced.

Overlap is detected on equal timestamps in canonical form, so requests on the same
quarter-hour grid and in the same timezone form (both naive or both with offset) are needed.

### SQLite persistence

On success, each timestep is stored as one row in the table `requested_aggregated_schedules`
in the database at `DB_PATH` (long table: `request_id`, `time_steps`, `power_rate_w`,
`requested_at` — UTC timestamp shared by all rows of the request, `blockchain_address` of
the buyer, `is_real_world_test`). Each assigned seller gets one row in
`power_request_assignments` (`request_id`, `seller_blockchain_address`, `share`, `trade_ids` as JSON list, `volume_kwh` and `max_power_w` the seller's trades allow), and each
market period the assignment is based on one row in `power_request_market_periods`
(`request_id`, `market_id`, `start_time`). All are written in one transaction; if it fails,
nothing is stored.

### Canonical timestamp form

`time_steps` are normalised before they are stored and handed out, so that one instant has
exactly one spelling:

- Timezone-aware values are converted to UTC and rendered as ISO 8601 with an explicit
  `+00:00` offset (`2026-05-08T10:00:00+00:00`).
- Timezone-naive values stay naive (`2026-05-08T12:00:00`); their zone is unknown and is
  not invented.

Because the form is ISO 8601 with a uniform offset, sorting the stored strings
lexicographically also sorts them chronologically.

### Error responses

| Status | When |
| ------ | ---- |
| `401` | Invalid HTTP Basic password |
| `422` | Validation failed (body or `is_real_world_test` rules), or `trade_ids` unknown, not trades of the buyer, or not covering the period of the schedule, or the schedule exceeds the storage limits |
| `500` | `POWER_REQUEST_PASSWORD` not configured, or SQLite read/persist failed |

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
    "blockchain_address": "0x...",
    "trade_ids": ["0b6f5c1e-8a4d-4e7b-9f3a-2c1d5e6a7b80"],
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
    "blockchain_address": "0x...",
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

## Endpoint: `GET /power_request`

Polled by the sellers (e.g. every 30 s) to learn which schedules they have to fulfil.

- **Authentication:** HTTP Basic Auth (username is ignored; password must match `POWER_REQUEST_PASSWORD`).
- **Query parameter:** `blockchain_address` of the seller.
- **Response:** `200 OK` with a list of every schedule assigned to the seller that it has not
  yet validly reported via `POST /actual_schedule`. The same schedule is returned on every
  poll until then, so use `request_id` to recognise schedules you already know.
  Schedules sent with `is_real_world_test=true` are left out once their last time step is
  over (server time, using the schedule's timezone when present). Simulated schedules lie in
  the past on purpose and stay in the list until they are reported or their market period
  is cleared again (see `POST /market_results`).

`trade_ids` are the seller's trades this schedule was assigned from; the seller has to
send exactly these back to `POST /actual_schedule`. `power_rate_w` is the seller's share
of the requested power (W):

```json
[
  {
    "request_id": "200312c0-cb21-479d-82ec-037077f0bc44",
    "trade_ids": ["0b6f5c1e-8a4d-4e7b-9f3a-2c1d5e6a7b80"],
    "time_steps": ["2026-05-08T12:00:00", "2026-05-08T12:15:00"],
    "power_rate_w": [5.0, 245.0]
  }
]
```

| Status | When |
| ------ | ---- |
| `401` | Invalid HTTP Basic password |
| `422` | `blockchain_address` missing |
| `500` | SQLite read failed |

```bash
curl -u seller:secret "http://localhost:8000/power_request?blockchain_address=0x..."
```

## Endpoint: `POST /actual_schedule`

Reported by a seller after driving its share of a schedule. The service validates the
actually driven schedule against the seller's share of the original request (matched by
`request_id` and the seller's `blockchain_address`), stores it, and returns the validation
result synchronously.

- **Authentication:** HTTP Basic Auth (username is ignored; password must match `POWER_REQUEST_PASSWORD`).
- **Body:** `request_id`, the seller's `blockchain_address`, `trade_ids`, `time_steps`, and `power_rate_w` (equal lengths).
- **Response:** `200 OK` with the validation result as JSON.

### Request body schema

```json
{
  "request_id": "200312c0-cb21-479d-82ec-037077f0bc44",
  "blockchain_address": "0x...",
  "trade_ids": ["0b6f5c1e-8a4d-4e7b-9f3a-2c1d5e6a7b80"],
  "time_steps": ["2026-05-08 12:00:00", "2026-05-08 12:15:00"],
  "power_rate_w": [5.0, 245.0]
}
```

`request_id` must be one handed out to this seller by `GET /power_request`, `trade_ids`
must be exactly the set of trades handed out with it (otherwise `valid: false`), and
`time_steps` must denote the same instants as the stored ones.

The comparison is made on the canonical form described above, not on the raw text, so any
ISO 8601 spelling of the right instant is accepted — `T` or a space as separator, and any
UTC offset. A timestamp that cannot be parsed as ISO 8601 is reported as a validation
error (`200 OK` with `valid: false`), not as a `422`.

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

On business-validation failure (unknown `request_id`, request not assigned to this seller,
`trade_ids` mismatch, `time_steps` mismatch, power or energy above what the seller's trades
allow) — still `200 OK`:

```json
{
  "request_id": "200312c0-cb21-479d-82ec-037077f0bc44",
  "valid": false,
  "errors": ["time_steps mismatch: expected [...], got [...]."]
}
```

`missing_energy_wh` is the missing discharge energy (Wh) summed over timesteps where the
seller's share of the requested power is negative and the actual power is below it
(see `calc_missing_energy_timeseries` in `util.py`).

### SQLite persistence

What gets stored in the table `actual_schedules` at `DB_PATH` depends on the validation
outcome:

- **Valid** (`time_steps` match the stored request): one row per timestep, with
  `time_steps`, `power_rate_w` (actual), and the per-timestep `missing_energy_wh`.
- **Invalid** (unknown `request_id`, not assigned to this seller, `trade_ids` mismatch, `time_steps` mismatch, storage limits exceeded): a single summary row with
  `time_steps`/`power_rate_w`/`missing_energy_wh` set to `NULL` and `validation_errors` filled in.

All rows have `request_id`, `blockchain_address` of the seller, `received_at` (UTC timestamp
of this submission), and `valid`.

### Error responses

| Status | When |
| ------ | ---- |
| `401` | Invalid HTTP Basic password |
| `422` | Body does not match the schema (e.g. `power_rate_w`/`time_steps` length mismatch) |
| `200` with `valid: false` | Unknown `request_id`, not assigned to this seller, `trade_ids` mismatch, `time_steps` mismatch, storage limits exceeded, or an unparseable timestamp |
| `500` | SQLite read/persist failed |

### curl example

```bash
curl -u bob:secret \
  -X POST "http://localhost:8000/actual_schedule" \
  -H "Content-Type: application/json" \
  -d '{
    "request_id": "200312c0-cb21-479d-82ec-037077f0bc44",
    "blockchain_address": "0x...",
    "time_steps": ["2026-05-08 12:00:00", "2026-05-08 12:15:00"],
    "power_rate_w": [5.0, 245.0]
  }'
```

## Endpoint: `POST /market_results`

Sent by ASSUME after every clearing of the battery market. Tells the service who was
awarded how much, for which delivery period, and with which counterparties.

- **Authentication:** HTTP Basic Auth with username `assume` and password `MARKET_RESULTS_PASSWORD`.
- **Body:** `market_id`, the delivery period of the cleared product (`start_time`,
  `end_time`), and the list of accepted `orders`. Rejected orders are not sent; the list is
  empty if nothing was traded.
- **Response:** `200 OK` with the number of stored orders.

### Request body schema

```json
{
  "market_id": "BatteryMarket",
  "start_time": "2023-05-09T01:00:00",
  "end_time": "2023-05-16T01:00:00",
  "orders": [
    {
      "bid_id": "B_01_1",
      "unit_id": "B_01",
      "blockchain_address": "0x...",
      "start_time": "2023-05-09T01:00:00",
      "end_time": "2023-05-16T01:00:00",
      "accepted_volume": -2.0,
      "accepted_price": 0.15,
      "c_rate": 1,
      "matches": [
        {"trade_id": "0b6f5c1e-8a4d-4e7b-9f3a-2c1d5e6a7b80", "bid_id": "S_01_1", "blockchain_address": "0x...", "volume": 0.5, "price": 0.15},
        {"trade_id": "7d2e9a14-3b5c-4f60-8a1e-9c4b2d7f1a35", "bid_id": "S_02_1", "blockchain_address": "0x...", "volume": 1.5, "price": 0.15}
      ]
    }
  ]
}
```

`accepted_volume` is positive for sellers (storage providers) and negative for buyers.
`matches` lists the counterparties of the order with the matched (positive) volume and the
uniform clearing price. Each match is one trade and carries a `trade_id` (UUID); buyer and
seller of the same trade receive the same `trade_id`.

### Response

```json
{
  "market_id": "BatteryMarket",
  "stored_orders": 1
}
```

### SQLite persistence

Each accepted order is stored as one row in the table `market_results` (`market_id`,
`bid_id`, `unit_id`, `blockchain_address`, `start_time`, `end_time`, `accepted_volume`,
`accepted_price`, `c_rate`, `matches` as JSON, `received_at`). `start_time`/`end_time` use the
canonical timestamp form described above.

A result for a `market_id` and delivery `start_time` that is already stored (e.g. from a
re-run of the simulation) is replaced, not added to, also if the new result has no accepted
orders. The power requests that were assigned from the old result are deleted with it, since
their assignment is outdated; buyers have to send them again. Deletes and insert happen in
one transaction.

### Error responses

| Status | When |
| ------ | ---- |
| `401` | Invalid HTTP Basic username or password |
| `422` | Body does not match the schema |
| `500` | SQLite persist failed |
