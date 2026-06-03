# SPDX-FileCopyrightText: MAS4TE Developers
#
# SPDX-License-Identifier: MIT

from datetime import datetime

import pandas as pd
from datetime import timedelta

import sqlite3


from models import SERIES_NAMES
from models import TimeseriesPayload


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
