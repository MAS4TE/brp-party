# SPDX-FileCopyrightText: MAS4TE Developers
#
# SPDX-License-Identifier: MIT

from datetime import datetime

import pandas as pd
from pydantic import BaseModel, ValidationInfo, field_validator
from datetime import timedelta


ALLOWED_SERIES = {"eeg", "wholesale", "community", "home"}
SERIES_NAMES = ("eeg", "wholesale", "community", "home")


class TimeseriesPayload(BaseModel):
    time_steps: list[datetime]
    eeg: None | list[float] = None
    wholesale: None | list[float] = None
    community: None | list[float] = None
    home: None | list[float] = None

    @field_validator("time_steps", mode="before")
    @classmethod
    def _check_time_steps_sorted(cls, value: list[datetime]) -> list[datetime]:
        for prev, cur in zip(value, value[1:]):
            try:
                if cur <= prev:
                    raise ValueError(
                        "time_steps must be strictly increasing (sorted ascending, no duplicates)."
                    )
            except TypeError as exc:
                raise ValueError(
                    "time_steps must be comparable datetimes (do not mix tz-aware and tz-naive values)."
                ) from exc
        return value

    @field_validator("time_steps", mode="before")
    @classmethod
    def _check_time_steps_is_datetime(cls, value: list[datetime]) -> list[datetime]:
        for ts in value:
            if isinstance(ts, datetime):
                continue
            try:
                datetime.fromisoformat(ts)
            except (TypeError, ValueError):
                raise ValueError("time_steps must be a list of datetimes.")
        return value

    @field_validator(*SERIES_NAMES, mode="before")
    @classmethod
    def _check_series_length(
        cls, value: list[float] | None, info: ValidationInfo
    ) -> list[float] | None:
        if value is None:
            return value
        timestamps = info.data.get("time_steps")
        if timestamps is not None and len(value) != len(timestamps):
            raise ValueError(
                f"Length mismatch for series '{info.field_name}': "
                f"got {len(value)}, expected {len(timestamps)}."
            )
        return value


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
