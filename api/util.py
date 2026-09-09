# SPDX-FileCopyrightText: MAS4TE Developers
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd

from models import SERIES_NAMES, ActualSchedulePayload, TimeseriesPayload

logger = logging.getLogger(__name__)


def require_env_vars(name_value_pairs: list[tuple[str, str | None]]) -> None:
    missing = [name for name, value in name_value_pairs if not value]
    if missing:
        raise SystemExit(
            f"Missing required environment variables: {', '.join(missing)}"
        )


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
    mqtt_timesteps = pd.to_datetime(payload.time_steps).astype(str).tolist()
    return mqtt_timesteps, power_rate_w


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


def write_requested_aggregated_schedule_to_db(
    conn: sqlite3.Connection, df: pd.DataFrame
) -> None:
    requested_at = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO requested_aggregated_schedules
        (request_id, time_steps, power_rate_w, requested_at)
        VALUES (?, ?, ?, ?)
        """,
        [
            (request_id, time_steps, power_rate_w, requested_at)
            for request_id, time_steps, power_rate_w in df[
                ["request_id", "time_steps", "power_rate_w"]
            ].itertuples(index=False, name=None)
        ],
    )
    conn.commit()


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


def write_valid_actual_schedule_to_db(
    conn: sqlite3.Connection,
    request_id: str,
    received_at: str,
    schedule: pd.DataFrame,
) -> None:
    """Store one row per timestep, including the per-timestep missing energy."""
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO actual_schedules
        (request_id, time_steps, power_rate_w, missing_energy_wh, received_at, valid, validation_errors)
        VALUES (?, ?, ?, ?, ?, 1, '[]')
        """,
        [
            (request_id, time_steps, power_rate_w, missing_energy_wh, received_at)
            for time_steps, power_rate_w, missing_energy_wh in schedule[
                ["time_steps", "power_rate_w", "missing_energy_wh"]
            ].itertuples(index=False, name=None)
        ],
    )
    conn.commit()


def write_invalid_actual_schedule_to_db(
    conn: sqlite3.Connection,
    request_id: str,
    received_at: str,
    errors: list[str],
) -> None:
    """Store a single summary row; there is no valid timeseries to persist per-timestep."""
    conn.execute(
        """
        INSERT INTO actual_schedules
        (request_id, time_steps, power_rate_w, missing_energy_wh, received_at, valid, validation_errors)
        VALUES (?, NULL, NULL, NULL, ?, 0, ?)
        """,
        (request_id, received_at, json.dumps(errors)),
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
    missing_energy_wh = (diff.abs() * 0.25).where(is_underdelivered_discharge, 0.0)

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
    actual_schedule = pd.DataFrame(
        {
            "time_steps": payload.time_steps,
            "power_rate_w": payload.power_rate_w,
        }
    )

    requested_schedule = pd.read_sql_query(
        """
        SELECT time_steps, power_rate_w
        FROM requested_aggregated_schedules
        WHERE request_id = ?
        ORDER BY time_steps
        """,
        conn,
        params=(request_id,),
    )

    # check for a matching original schedule before comparing time_steps, since an
    # empty requested_schedule would otherwise always look like a time_steps mismatch
    errors: list[str] = []
    if requested_schedule.empty:
        errors.append(f"No original schedule found for request_id {request_id!r}.")
    elif (
        actual_schedule["time_steps"].tolist()
        != requested_schedule["time_steps"].tolist()
    ):
        errors.append(
            f"time_steps mismatch: expected {requested_schedule['time_steps'].tolist()}, "
            f"got {actual_schedule['time_steps'].tolist()}."
        )

    if errors:
        write_invalid_actual_schedule_to_db(conn, request_id, received_at, errors)
        return {"request_id": request_id, "valid": False, "errors": errors}

    missing_energy_timeseries = calc_missing_energy_timeseries(
        actual_schedule, requested_schedule
    )
    write_valid_actual_schedule_to_db(
        conn, request_id, received_at, missing_energy_timeseries
    )

    return {
        "request_id": request_id,
        "valid": True,
        "errors": [],
        "missing_energy_wh": missing_energy_timeseries["missing_energy_wh"].sum(),
    }
