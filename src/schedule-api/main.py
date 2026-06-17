# SPDX-FileCopyrightText: MAS4TE Developers
#
# SPDX-License-Identifier: MIT

"""FastAPI endpoint to aggregate 1–4 timeseries and publish via MQTT."""

from __future__ import annotations

import logging
import os
import secrets
from typing import Annotated
from uuid import uuid4
import json
from contextlib import asynccontextmanager
import sqlite3

import paho.mqtt.publish as mqtt_publish
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import pandas as pd

import util
from models import TimeseriesPayload, SERIES_NAMES

load_dotenv()

logger = logging.getLogger(__name__)

API_PASSWORD = os.getenv("API_PASSWORD")

MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_PORT = os.getenv("MQTT_PORT")
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
REQUESTING_SCHEDULE_TOPIC = os.getenv("REQUESTING_SCHEDULE_TOPIC")

DB_PATH = os.getenv("DB_PATH")

util.require_env_vars(
    [
        ("API_PASSWORD", API_PASSWORD),
        ("MQTT_HOST", MQTT_HOST),
        ("MQTT_PORT", MQTT_PORT),
        ("MQTT_USER", MQTT_USER),
        ("MQTT_PASSWORD", MQTT_PASSWORD),
        ("REQUESTING_SCHEDULE_TOPIC", REQUESTING_SCHEDULE_TOPIC),
        ("DB_PATH", DB_PATH),
    ]
)
MQTT_PORT = int(MQTT_PORT)


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = sqlite3.connect(DB_PATH)
    util.create_table(conn)
    conn.close()
    yield


app = FastAPI(
    title="schedule-api",
    version="0.1.2",
    lifespan=lifespan,
)
security = HTTPBasic()


def verify_password(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> str:
    if not API_PASSWORD:
        logger.error("API_PASSWORD env var is not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_PASSWORD env var is not configured.",
        )
    if not secrets.compare_digest(
        credentials.password.encode("utf-8"), API_PASSWORD.encode("utf-8")
    ):
        logger.warning("Invalid credentials for user '%s'", credentials.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
    logger.info("Authenticated request for user '%s'", credentials.username)
    return credentials.username


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
        try:
            util.validate_real_world_test(payload)
        except ValueError as exc:
            logger.warning("Rejected power request: %s", exc)
            raise _bad_request(str(exc)) from exc

    n = len(payload.time_steps)
    for ts_name in util.SERIES_NAMES:
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
        for ts_name in SERIES_NAMES
        if getattr(payload, ts_name) is not None
    }
    logger.info(
        "Aggregated %d series for user '%s': %s",
        len(series_dict),
        user,
        ", ".join(series_dict),
    )

    mqtt_timesteps, power_rate_w = util.aggregate_power_series(payload)
    request_id = str(uuid4())
    mqtt_payload = {
        "request_id": request_id,
        "time_steps": mqtt_timesteps,
        "power_rate_w": power_rate_w,
    }
    auth = {"username": MQTT_USER, "password": MQTT_PASSWORD}

    database_df = pd.DataFrame(mqtt_payload)

    conn = sqlite3.connect(DB_PATH)
    try:
        util.write_requested_aggregated_schedule_to_db(conn, database_df)
    except Exception as e:
        logger.error("Could not persist schedule to database: %s", e)
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=500) from e

    try:
        mqtt_publish.single(
            topic=REQUESTING_SCHEDULE_TOPIC,
            payload=json.dumps(mqtt_payload),
            qos=1,
            hostname=MQTT_HOST,
            port=MQTT_PORT,
            auth=auth,
            tls={},
        )
    except Exception as e:
        logger.error(
            "Schedule persisted but MQTT publish failed for request_id=%s: %s",
            request_id,
            e,
        )
        raise HTTPException(status_code=500) from e
    finally:
        conn.close()

    return Response(status_code=status.HTTP_200_OK)
