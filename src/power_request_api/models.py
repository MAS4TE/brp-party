# SPDX-FileCopyrightText: MAS4TE Developers
#
# SPDX-License-Identifier: MIT

from datetime import datetime
from pydantic import BaseModel, ValidationInfo, field_validator

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
