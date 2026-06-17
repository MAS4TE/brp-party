# SPDX-FileCopyrightText: MAS4TE Developers
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

import pandas as pd
from pydantic import BaseModel, ValidationError, ValidationInfo, field_validator

logger = logging.getLogger(__name__)


def require_env_vars(name_value_pairs: list[tuple[str, str | None]]) -> None:
    missing = [name for name, value in name_value_pairs if not value]
    if missing:
        raise SystemExit(
            f"Missing required environment variables: {', '.join(missing)}"
        )


class ActualSchedulePayload(BaseModel):
    request_id: str
    time_steps: list[str]
    power_rate_w: list[float]

    @field_validator("power_rate_w")
    @classmethod
    def _check_lengths_match(
        cls, value: list[float], info: ValidationInfo
    ) -> list[float]:
        time_steps = info.data.get("time_steps")
        if time_steps is not None and len(value) != len(time_steps):
            raise ValueError(
                f"power_rate_w length ({len(value)}) must match "
                f"time_steps length ({len(time_steps)})."
            )
        return value


def compare_schedules(
    actual_schedule: pd.DataFrame,
    requested_schedule: pd.DataFrame,
) -> list[str]:
    errors: list[str] = []

    if len(actual_schedule) != len(requested_schedule):
        errors.append(
            f"schedule length mismatch: expected {len(requested_schedule)}, "
            f"got {len(actual_schedule)}."
        )

    actual_schedule.sort_values("time_steps", inplace=True)

    if (
        actual_schedule["time_steps"].tolist()
        != requested_schedule["time_steps"].tolist()
    ):
        errors.append(
            f"time_steps mismatch: expected {requested_schedule['time_steps'].tolist()}, "
            f"got {actual_schedule['time_steps'].tolist()}."
        )

    return errors


def load_requested_schedule(conn: sqlite3.Connection, request_id: str) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT time_steps, power_rate_w
        FROM requested_aggregated_schedules
        WHERE request_id = ?
        ORDER BY time_steps
        """,
        conn,
        params=(request_id,),
    )


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


def process_actual_schedule(conn: sqlite3.Connection, raw_payload: bytes) -> dict:
    # check if payload is valid JSON
    try:
        data = json.loads(raw_payload)
    except json.JSONDecodeError:
        return {"request_id": "", "valid": False, "errors": ["Invalid JSON payload."]}

    # check if payload is valid ActualSchedulePayload
    try:
        payload = ActualSchedulePayload.model_validate(data)

    except ValidationError as exc:
        request_id = ""
        if isinstance(data, dict) and isinstance(data.get("request_id"), str):
            request_id = data["request_id"]

        return {
            "request_id": request_id,
            "valid": False,
            "errors": [error["msg"] for error in exc.errors()],
        }

    request_id = payload.request_id
    actual_schedule = pd.DataFrame()
    actual_schedule["time_steps"] = payload.time_steps
    actual_schedule["power_rate_w"] = payload.power_rate_w
    actual_schedule["request_id"] = request_id

    # retrieve original schedule
    sql = """
        SELECT time_steps, power_rate_w
        FROM requested_aggregated_schedules
        WHERE request_id = ?
        ORDER BY time_steps
    """
    try:
        requested_schedule = pd.read_sql_query(
            sql,
            conn,
            params=(request_id,),
        )
    except Exception:
        logger.exception("Failed to load original schedule from database")
        return {
            "request_id": request_id,
            "valid": False,
            "errors": ["Internal validation error."],
        }

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
        try:
            missing_discharge_energy = calc_schedule_differences(
                actual_schedule, requested_schedule
            )
        except Exception:
            logger.exception("Failed to calculate schedule differences")
            errors.append("Internal validation error.")

    try:
        write_actual_schedule_to_db(
            conn, actual_schedule, valid=not errors, errors=errors
        )
    except Exception:
        logger.exception("Failed to save actual schedule to database")
        return {
            "request_id": request_id,
            "valid": False,
            "errors": ["Internal validation error."],
        }

    if errors:
        return {"request_id": request_id, "valid": False, "errors": errors}

    return {
        "request_id": request_id,
        "valid": True,
        "errors": [],
        "missing_energy_wh": missing_discharge_energy,
    }
