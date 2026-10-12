# SPDX-FileCopyrightText: MAS4TE Developers
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import pandas as pd

from models import (
    SERIES_NAMES,
    TIME_STEP,
    ActualSchedulePayload,
    MarketResultsPayload,
    TimeseriesPayload,
)

logger = logging.getLogger(__name__)


def require_env_vars(name_value_pairs: list[tuple[str, str | None]]) -> None:
    missing = [name for name, value in name_value_pairs if not value]
    if missing:
        raise SystemExit(
            f"Missing required environment variables: {', '.join(missing)}"
        )


def canonical_time_steps(time_steps: Sequence[datetime | str]) -> list[str]:
    """Render timestamps in the one textual form used on the wire, in the database and
    for every comparison.

    Accepts datetimes as well as strings in any ISO 8601 spelling (``T`` or a space as
    separator, any offset); accepting all of them is what makes comparisons independent
    of how the sender wrote them. Timezone-aware values are converted to UTC so that two
    spellings of the same instant produce the same string. Naive values stay naive:
    their zone is unknown and must not be invented. The result is ISO 8601, so sorting
    the strings lexicographically also sorts them chronologically.

    Raises ValueError naming the offending value if a string is not ISO 8601.
    """
    canonical = []
    for step in time_steps:
        if isinstance(step, str):
            try:
                step = datetime.fromisoformat(step)
            except ValueError as exc:
                raise ValueError(
                    f"time_steps contains a value that is not an ISO 8601 datetime: {step!r}."
                ) from exc
        if step.tzinfo is not None:
            step = step.astimezone(timezone.utc)
        canonical.append(step.isoformat())
    return canonical


def aggregate_power_series(payload: TimeseriesPayload) -> tuple[list[str], list[float]]:
    """Sum provided series per timestep and convert aggregated power from kW to W."""
    series_dict = {
        name: getattr(payload, name)
        for name in SERIES_NAMES
        if getattr(payload, name) is not None
    }
    df = pd.DataFrame(series_dict, index=payload.time_steps)
    aggregated_kw = df.sum(axis=1)
    power_rate_w = (aggregated_kw * 1000).tolist()
    return canonical_time_steps(payload.time_steps), power_rate_w


def is_quarter_hour_aligned(dt: datetime) -> bool:
    return dt.minute % 15 == 0 and dt.second == 0 and dt.microsecond == 0


def validate_real_world_test(payload: TimeseriesPayload) -> None:
    """Raise ValueError when real-world test constraints are not met."""
    start_time = payload.time_steps[0]
    end_time = payload.time_steps[-1]

    if not is_quarter_hour_aligned(start_time):
        raise ValueError(
            "start_time must be aligned to a quarter-hour (:00, :15, :30, :45)."
        )
    if len(payload.time_steps) > 1 and start_time >= end_time:
        raise ValueError("start_time must be strictly before end_time.")
    now = datetime.now(tz=start_time.tzinfo) if start_time.tzinfo else datetime.now()
    if start_time <= now + timedelta(minutes=15):
        raise ValueError("start_time must be more than 15 minutes in the future.")


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, col_type: str
) -> None:
    """Add `column` to `table` if it doesn't exist yet (idempotent schema migration)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()


def create_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS requested_aggregated_schedules (
            request_id TEXT,
            time_steps TEXT,
            power_rate_w REAL
        )
    """)
    conn.commit()
    _ensure_column(conn, "requested_aggregated_schedules", "requested_at", "TEXT")
    _ensure_column(conn, "requested_aggregated_schedules", "blockchain_address", "TEXT")
    _ensure_column(
        conn, "requested_aggregated_schedules", "is_real_world_test", "INTEGER"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS power_request_assignments (
            request_id TEXT,
            seller_blockchain_address TEXT,
            share REAL
        )
    """)
    # market periods whose results a request was assigned from
    conn.execute("""
        CREATE TABLE IF NOT EXISTS power_request_market_periods (
            request_id TEXT,
            market_id TEXT,
            start_time TEXT
        )
    """)
    conn.commit()


def write_requested_aggregated_schedule_to_db(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    blockchain_address: str,
    seller_shares: dict[str, float],
    market_periods: set[tuple[str, str]],
    is_real_world_test: bool,
) -> None:
    """Store the requested schedule, which seller fulfils which share of it and the
    market periods the assignment is based on.

    The new schedule replaces the buyer's older requests where they overlap: their
    time_steps that the new one also covers are deleted, the others stay.
    """
    requested_at = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    time_steps = df["time_steps"].tolist()
    cur.execute(
        f"""
        DELETE FROM requested_aggregated_schedules
        WHERE blockchain_address = ?
          AND time_steps IN ({", ".join("?" * len(time_steps))})
        """,
        (blockchain_address, *time_steps),
    )
    if cur.rowcount:
        logger.info(
            "New power request of blockchain_address=%s replaces %d time steps of older requests",
            blockchain_address,
            cur.rowcount,
        )
    cur.executemany(
        """
        INSERT INTO requested_aggregated_schedules
        (request_id, time_steps, power_rate_w, requested_at, blockchain_address, is_real_world_test)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                request_id,
                time_steps,
                power_rate_w,
                requested_at,
                blockchain_address,
                is_real_world_test,
            )
            for request_id, time_steps, power_rate_w in df[
                ["request_id", "time_steps", "power_rate_w"]
            ].itertuples(index=False, name=None)
        ],
    )
    request_id = df["request_id"].iloc[0]
    cur.executemany(
        """
        INSERT INTO power_request_assignments
        (request_id, seller_blockchain_address, share)
        VALUES (?, ?, ?)
        """,
        [(request_id, seller, share) for seller, share in seller_shares.items()],
    )
    cur.executemany(
        """
        INSERT INTO power_request_market_periods (request_id, market_id, start_time)
        VALUES (?, ?, ?)
        """,
        [
            (request_id, market_id, start_time)
            for market_id, start_time in market_periods
        ],
    )
    conn.commit()


def find_seller_shares(
    conn: sqlite3.Connection, buyer_address: str, time_steps: list[str]
) -> tuple[dict[str, float], set[tuple[str, str]]]:
    """Sellers the buyer was matched with for the period of the schedule, with
    the share of the schedule each of them fulfils (by matched volume), and the
    market periods (market_id, start_time) these matches come from.

    Empty if the buyer has no accepted order whose delivery period covers all
    time_steps.
    """
    first_step = datetime.fromisoformat(time_steps[0])
    last_step_end = datetime.fromisoformat(time_steps[-1]) + TIME_STEP

    rows = conn.execute(
        """
        SELECT market_id, bid_id, start_time, end_time, matches
        FROM market_results
        WHERE blockchain_address = ? AND accepted_volume < 0
        """,
        (buyer_address,),
    ).fetchall()

    seller_volumes: dict[str, float] = {}
    market_periods: set[tuple[str, str]] = set()
    for market_id, _, start_time, end_time, matches in rows:
        try:
            is_covered = datetime.fromisoformat(
                start_time
            ) <= first_step and last_step_end <= datetime.fromisoformat(end_time)
        except TypeError:
            # tz-aware schedule vs. tz-naive market result (or vice versa)
            is_covered = False
        if not is_covered:
            continue
        market_periods.add((market_id, start_time))
        for match in json.loads(matches):
            seller = match["blockchain_address"]
            seller_volumes[seller] = seller_volumes.get(seller, 0) + match["volume"]

    total_volume = sum(seller_volumes.values())
    seller_shares = {
        seller: volume / total_volume for seller, volume in seller_volumes.items()
    }
    return seller_shares, market_periods


def get_open_schedules(conn: sqlite3.Connection, seller_address: str) -> list[dict]:
    """Schedules assigned to the seller that it has not validly reported yet,
    each scaled to the seller's share.

    Real-world schedules whose last time step is over are left out; simulated ones
    lie in the past on purpose and stay until they are reported.
    """
    assignments = conn.execute(
        """
        SELECT a.request_id, a.share, (
            SELECT MAX(r.is_real_world_test) FROM requested_aggregated_schedules r
            WHERE r.request_id = a.request_id
        )
        FROM power_request_assignments a
        WHERE a.seller_blockchain_address = ?
          AND NOT EXISTS (
              SELECT 1 FROM actual_schedules s
              WHERE s.request_id = a.request_id
                AND s.blockchain_address = a.seller_blockchain_address
                AND s.valid = 1
          )
        """,
        (seller_address,),
    ).fetchall()

    open_schedules = []
    for request_id, share, is_real_world_test in assignments:
        schedule = read_requested_schedule(conn, request_id)
        # completely replaced by a newer request of the buyer
        if schedule.empty:
            continue
        if is_real_world_test:
            last_step = datetime.fromisoformat(schedule["time_steps"].iloc[-1])
            now = (
                datetime.now(tz=last_step.tzinfo)
                if last_step.tzinfo
                else datetime.now()
            )
            if last_step + TIME_STEP <= now:
                continue
        open_schedules.append(
            {
                "request_id": request_id,
                "time_steps": schedule["time_steps"].tolist(),
                "power_rate_w": (schedule["power_rate_w"] * share).tolist(),
            }
        )
    return open_schedules


def read_requested_schedule(conn: sqlite3.Connection, request_id: str) -> pd.DataFrame:
    requested_schedule = pd.read_sql_query(
        """
        SELECT time_steps, power_rate_w
        FROM requested_aggregated_schedules
        WHERE request_id = ?
        """,
        conn,
        params=(request_id,),
    )
    # Canonicalise the stored side too: rows written before this format existed still
    # carry pandas' space-separated spelling. Sorting then happens on the canonical
    # form, which is why the query above needs no ORDER BY.
    requested_schedule["time_steps"] = canonical_time_steps(
        requested_schedule["time_steps"].tolist()
    )
    return requested_schedule.sort_values("time_steps").reset_index(drop=True)


def create_actual_schedules_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS actual_schedules (
            request_id TEXT,
            time_steps TEXT,
            power_rate_w REAL,
            missing_energy_wh REAL,
            received_at TEXT,
            valid INTEGER,
            validation_errors TEXT
        )
    """)
    conn.commit()
    _ensure_column(conn, "actual_schedules", "missing_energy_wh", "REAL")
    _ensure_column(conn, "actual_schedules", "blockchain_address", "TEXT")


def write_valid_actual_schedule_to_db(
    conn: sqlite3.Connection,
    request_id: str,
    blockchain_address: str,
    received_at: str,
    schedule: pd.DataFrame,
) -> None:
    """Store one row per timestep, including the per-timestep missing energy."""
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO actual_schedules
        (request_id, blockchain_address, time_steps, power_rate_w, missing_energy_wh, received_at, valid, validation_errors)
        VALUES (?, ?, ?, ?, ?, ?, 1, '[]')
        """,
        [
            (
                request_id,
                blockchain_address,
                time_steps,
                power_rate_w,
                missing_energy_wh,
                received_at,
            )
            for time_steps, power_rate_w, missing_energy_wh in schedule[
                ["time_steps", "power_rate_w", "missing_energy_wh"]
            ].itertuples(index=False, name=None)
        ],
    )
    conn.commit()


def write_invalid_actual_schedule_to_db(
    conn: sqlite3.Connection,
    request_id: str,
    blockchain_address: str,
    received_at: str,
    errors: list[str],
) -> None:
    """Store a single summary row; there is no valid timeseries to persist per-timestep."""
    conn.execute(
        """
        INSERT INTO actual_schedules
        (request_id, blockchain_address, time_steps, power_rate_w, missing_energy_wh, received_at, valid, validation_errors)
        VALUES (?, ?, NULL, NULL, NULL, ?, 0, ?)
        """,
        (request_id, blockchain_address, received_at, json.dumps(errors)),
    )
    conn.commit()


def create_market_results_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_results (
            market_id TEXT,
            bid_id TEXT,
            unit_id TEXT,
            blockchain_address TEXT,
            start_time TEXT,
            end_time TEXT,
            accepted_volume REAL,
            accepted_price REAL,
            matches TEXT,
            received_at TEXT
        )
    """)
    conn.commit()


def write_market_results_to_db(
    conn: sqlite3.Connection, payload: MarketResultsPayload
) -> None:
    """Store one row per accepted order; the counterparties are kept as JSON.

    A result for a market and delivery period that is already stored (e.g. from a
    re-run of the simulation) replaces the stored one, also if the new result has
    no accepted orders. The power requests assigned from the old result are deleted
    with it, since their assignment is outdated.
    """
    received_at = datetime.now(timezone.utc).isoformat()
    market_period = (payload.market_id, canonical_time_steps([payload.start_time])[0])

    outdated_requests = """
        SELECT request_id FROM power_request_market_periods
        WHERE market_id = ? AND start_time = ?
    """
    for table in ("requested_aggregated_schedules", "power_request_assignments"):
        conn.execute(
            f"DELETE FROM {table} WHERE request_id IN ({outdated_requests})",
            market_period,
        )
    conn.execute(
        """
        DELETE FROM power_request_market_periods
        WHERE request_id IN (SELECT request_id FROM power_request_market_periods
                             WHERE market_id = ? AND start_time = ?)
        """,
        market_period,
    )
    conn.execute(
        "DELETE FROM market_results WHERE market_id = ? AND start_time = ?",
        market_period,
    )
    conn.executemany(
        """
        INSERT INTO market_results
        (market_id, bid_id, unit_id, blockchain_address, start_time, end_time,
         accepted_volume, accepted_price, matches, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                payload.market_id,
                order.bid_id,
                order.unit_id,
                order.blockchain_address,
                *canonical_time_steps([order.start_time, order.end_time]),
                order.accepted_volume,
                order.accepted_price,
                json.dumps([match.model_dump() for match in order.matches]),
                received_at,
            )
            for order in payload.orders
        ],
    )
    conn.commit()


def calc_missing_energy_timeseries(
    actual_schedule: pd.DataFrame,
    requested_schedule: pd.DataFrame,
) -> pd.DataFrame:
    """Per-timestep missing discharge energy (Wh) where requested power is negative
    and actual power under-delivers it."""
    actual = actual_schedule.sort_values("time_steps").reset_index(drop=True)
    requested = requested_schedule.sort_values("time_steps").reset_index(drop=True)

    merged = actual.merge(
        requested,
        on="time_steps",
        suffixes=("_actual", "_requested"),
    )
    diff = merged["power_rate_w_requested"] - merged["power_rate_w_actual"]
    is_underdelivered_discharge = (merged["power_rate_w_requested"] < 0) & (diff < 0)
    step_hours = TIME_STEP / timedelta(hours=1)
    missing_energy_wh = (diff.abs() * step_hours).where(
        is_underdelivered_discharge, 0.0
    )

    return pd.DataFrame(
        {
            "time_steps": merged["time_steps"],
            "power_rate_w": merged["power_rate_w_actual"],
            "missing_energy_wh": missing_energy_wh,
        }
    )


def validate_actual_schedule(
    conn: sqlite3.Connection, payload: ActualSchedulePayload
) -> dict:
    """Validate a fulfiller's actual schedule against the stored requested one.

    The payload is already schema-validated by FastAPI/Pydantic. Business-level
    validation outcomes (no matching request, time_steps mismatch) are returned as
    ``valid: false`` results; database failures propagate to the caller as exceptions.

    On success, a per-timestep timeseries (including missing energy) is persisted.
    On failure, only a single summary row with the validation errors is persisted.
    """
    received_at = datetime.now(timezone.utc).isoformat()
    request_id = payload.request_id
    seller = payload.blockchain_address

    # Compare canonical forms, not raw text: the fulfiller may spell a timestamp
    # differently and still mean the same instant. An unparseable timestamp is a
    # validation outcome, not a server error.
    try:
        actual_time_steps = canonical_time_steps(payload.time_steps)
    except ValueError as exc:
        write_invalid_actual_schedule_to_db(
            conn, request_id, seller, received_at, [str(exc)]
        )
        return {"request_id": request_id, "valid": False, "errors": [str(exc)]}

    actual_schedule = pd.DataFrame(
        {
            "time_steps": actual_time_steps,
            "power_rate_w": payload.power_rate_w,
        }
    )

    requested_schedule = read_requested_schedule(conn, request_id)
    share = conn.execute(
        """
        SELECT share FROM power_request_assignments
        WHERE request_id = ? AND seller_blockchain_address = ?
        """,
        (request_id, seller),
    ).fetchone()

    # check for a matching original schedule before comparing time_steps, since an
    # empty requested_schedule would otherwise always look like a time_steps mismatch
    errors: list[str] = []
    if requested_schedule.empty:
        errors.append(f"No original schedule found for request_id {request_id!r}.")
    elif share is None:
        errors.append(
            f"request_id {request_id!r} is not assigned to blockchain_address {seller!r}."
        )
    elif (
        actual_schedule["time_steps"].tolist()
        != requested_schedule["time_steps"].tolist()
    ):
        errors.append(
            f"time_steps mismatch: expected {requested_schedule['time_steps'].tolist()}, "
            f"got {actual_schedule['time_steps'].tolist()}."
        )

    if errors:
        write_invalid_actual_schedule_to_db(
            conn, request_id, seller, received_at, errors
        )
        return {"request_id": request_id, "valid": False, "errors": errors}

    # the seller only has to drive its share of the requested schedule
    requested_schedule["power_rate_w"] *= share[0]
    missing_energy_timeseries = calc_missing_energy_timeseries(
        actual_schedule, requested_schedule
    )
    write_valid_actual_schedule_to_db(
        conn, request_id, seller, received_at, missing_energy_timeseries
    )

    return {
        "request_id": request_id,
        "valid": True,
        "errors": [],
        "missing_energy_wh": missing_energy_timeseries["missing_energy_wh"].sum(),
    }
