"""FastAPI-Endpoint zum Aggregieren von 1–4 Timeseries und Publish via MQTT."""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Annotated

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ValidationInfo, field_validator

# from mqtt_publisher import MQTTConfig, publish as mqtt_publish_payload

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

ALLOWED_SERIES: frozenset[str] = frozenset(
    {"eeg", "wholesale", "community", "home"}
)

app = FastAPI(
    title="brp-schedule-api",
    version="0.1.1",
)
security = HTTPBasic()


def verify_password(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> str:
    expected = os.getenv("API_PASSWORD")

    if not expected:
        logger.error("API_PASSWORD env var is not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_PASSWORD env var is not configured.",
        )
    if not secrets.compare_digest(
        credentials.password.encode("utf-8"), expected.encode("utf-8")
    ):
        logger.warning("Invalid credentials for user '%s'", credentials.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
    logger.info("Authenticated request for user '%s'", credentials.username)
    return credentials.username


class TimeseriesPayload(BaseModel):
    time_steps: list[datetime]
    eeg: None | list[float] = None
    wholesale: None | list[float] = None
    community: None | list[float] = None
    home: None | list[float] = None

    @field_validator("time_steps", mode="before")
    @classmethod
    def _check_time_steps_sorted(cls, value: list[datetime]) -> list[datetime]:
        # Ensure timestamps are strictly increasing; FastAPI will surface this as 422.
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
            try:
                datetime.fromisoformat(ts)
            except ValueError:
                raise ValueError(
                    "time_steps must be a list of datetimes."
                )
        return value

    @field_validator("eeg", "wholesale", "community", "home", mode="before")
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


def _is_quarter_hour_aligned(dt: datetime) -> bool:
    return dt.minute % 15 == 0 and dt.second == 0 and dt.microsecond == 0


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
    )


@app.post("/power_request", status_code=status.HTTP_200_OK)
def power_request(
    payload: TimeseriesPayload,
    user: Annotated[str, Depends(verify_password)],
    is_real_world_test: bool = False,
):
    start_time = payload.time_steps[0]
    end_time = payload.time_steps[-1]
    logger.info(
        "Received power request from user '%s' with %d time steps (%s to %s)",
        user,
        len(payload.time_steps),
        start_time.isoformat(),
        end_time.isoformat(),
    )

    if is_real_world_test:
        if not _is_quarter_hour_aligned(start_time):
            logger.warning("Rejected power request: start_time is not quarter-hour aligned")
            raise _bad_request(
                "start_time must be aligned to a quarter-hour (:00, :15, :30, :45)."
            )
        if len(payload.time_steps) > 1 and start_time >= end_time:
            logger.warning("Rejected power request: start_time is not before end_time")
            raise _bad_request("start_time must be strictly before end_time.")
        now = datetime.now(tz=start_time.tzinfo) if start_time.tzinfo else datetime.now()
        if start_time <= now + timedelta(minutes=15):
            logger.warning(
                "Rejected power request: start_time is not far enough in the future"
            )
            raise _bad_request(
                "start_time must be more than 15 minutes in the future."
            )

    n = len(payload.time_steps)
    for ts_name in ["eeg", "wholesale", "community", "home"]:
        series = getattr(payload, ts_name)
        if series is not None and len(series) != n:
            logger.warning(
                "Rejected power request: length mismatch for series '%s' (got %d, expected %d)",
                ts_name,
                len(series),
                n,
            )
            raise _bad_request(
                f"Length mismatch for series '{ts_name}': "
                f"got {len(series)}, expected {n}."
            )

    series_dict = {
        ts_name: getattr(payload, ts_name)
        for ts_name in ["eeg", "wholesale", "community", "home"]
    }
    series_dict = {
        ts_name: series
        for ts_name, series in series_dict.items()
        if series is not None
    }
    df = pd.DataFrame(series_dict, index=payload.time_steps)
    df["aggregated"] = df.sum(axis=1)
    logger.info(
        "Aggregated %d series for user '%s': %s",
        len(series_dict),
        user,
        ", ".join(series_dict),
    )

    return df[["aggregated"]].to_json(orient="records")
