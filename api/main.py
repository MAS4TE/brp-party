# SPDX-FileCopyrightText: MAS4TE Developers
#
# SPDX-License-Identifier: MIT

"""FastAPI service: aggregate 1–4 timeseries of a buyer, assign them to the sellers
it was matched with in the market, hand them out to those sellers, and validate the
sellers' actual schedules against the stored request."""

from __future__ import annotations

import logging
import os
import secrets
from typing import Annotated
from uuid import uuid4
from contextlib import asynccontextmanager
import sqlite3

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import pandas as pd

import util
from models import (
    TimeseriesPayload,
    ActualSchedulePayload,
    MarketResultsPayload,
    PowerRequestResponse,
    SERIES_NAMES,
)

load_dotenv()

logger = logging.getLogger(__name__)

POWER_REQUEST_PASSWORD = os.getenv("POWER_REQUEST_PASSWORD")
# only ASSUME may send market results, so it gets its own password
MARKET_RESULTS_PASSWORD = os.getenv("MARKET_RESULTS_PASSWORD")

DB_PATH = os.getenv("DB_PATH")

util.require_env_vars(
    [
        ("POWER_REQUEST_PASSWORD", POWER_REQUEST_PASSWORD),
        ("MARKET_RESULTS_PASSWORD", MARKET_RESULTS_PASSWORD),
        ("DB_PATH", DB_PATH),
    ]
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = sqlite3.connect(DB_PATH)
    util.create_table(conn)
    util.create_actual_schedules_table(conn)
    util.create_market_results_table(conn)
    conn.close()
    yield


app = FastAPI(
    title="brp-party", version="0.2.0", lifespan=lifespan, root_path="/brp-api/"
)
security = HTTPBasic()


def verify_password(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> str:
    if not POWER_REQUEST_PASSWORD:
        logger.error("POWER_REQUEST_PASSWORD env var is not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="POWER_REQUEST_PASSWORD env var is not configured.",
        )
    if not secrets.compare_digest(
        credentials.password.encode("utf-8"), POWER_REQUEST_PASSWORD.encode("utf-8")
    ):
        logger.warning("Invalid credentials for user '%s'", credentials.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
    logger.info("Authenticated request for user '%s'", credentials.username)
    return credentials.username


@app.post(
    "/power_request",
    status_code=status.HTTP_200_OK,
    response_model=PowerRequestResponse,
)
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
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
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

    time_steps, power_rate_w = util.aggregate_power_series(payload)
    request_id = str(uuid4())
    database_df = pd.DataFrame(
        {
            "request_id": request_id,
            "time_steps": time_steps,
            "power_rate_w": power_rate_w,
        }
    )

    conn = sqlite3.connect(DB_PATH)
    try:
        # only buyers that were awarded storage for this period may send a schedule
        seller_shares, market_periods = util.find_seller_shares(
            conn, payload.blockchain_address, time_steps
        )
        if not seller_shares:
            logger.warning(
                "Rejected power request: no awarded storage for blockchain_address=%s",
                payload.blockchain_address,
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"No awarded storage for blockchain_address "
                    f"{payload.blockchain_address!r} in the period of the schedule."
                ),
            )

        util.write_requested_aggregated_schedule_to_db(
            conn,
            database_df,
            payload.blockchain_address,
            seller_shares,
            market_periods,
            is_real_world_test,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Could not persist schedule to database: %s", e)
        conn.rollback()
        raise HTTPException(status_code=500) from e
    finally:
        conn.close()

    logger.info(
        "Stored power request %s, assigned to sellers: %s",
        request_id,
        seller_shares,
    )
    return PowerRequestResponse(request_id=request_id)


@app.get("/power_request", status_code=status.HTTP_200_OK)
def power_requests(
    blockchain_address: str,
    user: Annotated[str, Depends(verify_password)],
):
    """Polled by the sellers: the schedules they still have to fulfil."""
    conn = sqlite3.connect(DB_PATH)
    try:
        open_schedules = util.get_open_schedules(conn, blockchain_address)
    except Exception as e:
        logger.error(
            "Failed to read open schedules for blockchain_address=%s: %s",
            blockchain_address,
            e,
        )
        raise HTTPException(status_code=500) from e
    finally:
        conn.close()

    return open_schedules


@app.post("/actual_schedule", status_code=status.HTTP_200_OK)
def actual_schedule(
    payload: ActualSchedulePayload,
    user: Annotated[str, Depends(verify_password)],
):
    logger.info(
        "Received actual schedule from user '%s' for request_id=%s with %d time steps",
        user,
        payload.request_id,
        len(payload.time_steps),
    )

    conn = sqlite3.connect(DB_PATH)
    try:
        result = util.validate_actual_schedule(conn, payload)
    except Exception as e:
        logger.error(
            "Failed to validate actual schedule for request_id=%s: %s",
            payload.request_id,
            e,
        )
        conn.rollback()
        raise HTTPException(status_code=500) from e
    finally:
        conn.close()

    logger.info(
        "Validated actual schedule for request_id=%s: valid=%s",
        payload.request_id,
        result["valid"],
    )
    return result


@app.post("/market_results", status_code=status.HTTP_200_OK)
def market_results(
    payload: MarketResultsPayload,
    x_market_results_password: Annotated[str | None, Header()] = None,
):
    # only ASSUME knows this password; no username needed
    if x_market_results_password is None or not secrets.compare_digest(
        x_market_results_password.encode("utf-8"),
        MARKET_RESULTS_PASSWORD.encode("utf-8"),
    ):
        logger.warning("Invalid market results password")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
        )

    logger.info(
        "Received %d accepted orders of market '%s'",
        len(payload.orders),
        payload.market_id,
    )

    conn = sqlite3.connect(DB_PATH)
    try:
        util.write_market_results_to_db(conn, payload)
    except Exception as e:
        logger.error(
            "Could not persist market results of market '%s': %s",
            payload.market_id,
            e,
        )
        conn.rollback()
        raise HTTPException(status_code=500) from e
    finally:
        conn.close()

    return {"market_id": payload.market_id, "stored_orders": len(payload.orders)}
