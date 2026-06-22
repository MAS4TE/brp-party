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


def create_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS requested_aggregated_schedules (
            request_id TEXT,
            time_steps TEXT,
            power_rate_w REAL
        )
    """)
    conn.commit()


def write_requested_aggregated_schedule_to_db(
    conn: sqlite3.Connection, df: pd.DataFrame
) -> None:
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO requested_aggregated_schedules
        (request_id, time_steps, power_rate_w)
        VALUES (?, ?, ?)
        """,
        df[["request_id", "time_steps", "power_rate_w"]].itertuples(
            index=False, name=None
        ),
    )
    conn.commit()


def create_actual_schedules_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS actual_schedules (
            request_id TEXT,
            time_steps TEXT,
            power_rate_w REAL,
            received_at TEXT,
            valid INTEGER,
            validation_errors TEXT
        )
    """)
    conn.commit()


def write_actual_schedule_to_db(
    conn: sqlite3.Connection,
    actual_schedule: pd.DataFrame,
    valid: bool,
    errors: list[str],
) -> None:
    received_at = datetime.now(timezone.utc).isoformat()
    validation_errors = json.dumps(errors)
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO actual_schedules
        (request_id, time_steps, power_rate_w, received_at, valid, validation_errors)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                request_id,
                time_steps,
                power_rate_w,
                received_at,
                int(valid),
                validation_errors,
            )
            for request_id, time_steps, power_rate_w in actual_schedule[
                ["request_id", "time_steps", "power_rate_w"]
            ].itertuples(index=False, name=None)
        ],
    )
    conn.commit()


def calc_schedule_differences(
    actual_schedule: pd.DataFrame,
    requested_schedule: pd.DataFrame,
) -> float:
    """Sum missing discharge energy (Wh) for timesteps with negative requested power."""
    actual = actual_schedule.sort_values("time_steps").reset_index(drop=True)
    requested = requested_schedule.sort_values("time_steps").reset_index(drop=True)

    merged = actual.merge(
        requested,
        on="time_steps",
        suffixes=("_actual", "_requested"),
    )
    discharge_steps = merged[merged["power_rate_w_requested"] < 0]

    discharge_steps["diff"] = (
        discharge_steps["power_rate_w_requested"]
        - discharge_steps["power_rate_w_actual"]
    )
    missing_discharge_power = abs(
        discharge_steps[discharge_steps["diff"] < 0]["diff"].sum()
    )
    missing_discharge_energy = 0.25 * missing_discharge_power

    return missing_discharge_energy


def validate_actual_schedule(
    conn: sqlite3.Connection, payload: ActualSchedulePayload
) -> dict:
    """Validate a fulfiller's actual schedule against the stored requested one.

    The payload is already schema-validated by FastAPI/Pydantic. Business-level
    validation outcomes (no matching request, time_steps mismatch) are returned as
    ``valid: false`` results; database failures propagate to the caller as exceptions.
    """
    request_id = payload.request_id
    actual_schedule = pd.DataFrame(
        {
            "time_steps": payload.time_steps,
            "power_rate_w": payload.power_rate_w,
            "request_id": request_id,
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

    missing_discharge_energy = None
    if not errors:
        missing_discharge_energy = calc_schedule_differences(
            actual_schedule, requested_schedule
        )

    write_actual_schedule_to_db(conn, actual_schedule, valid=not errors, errors=errors)

    if errors:
        return {"request_id": request_id, "valid": False, "errors": errors}

    return {
        "request_id": request_id,
        "valid": True,
        "errors": [],
        "missing_energy_wh": missing_discharge_energy,
    }
