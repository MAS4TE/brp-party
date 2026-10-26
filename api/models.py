# SPDX-FileCopyrightText: MAS4TE Developers
#
# SPDX-License-Identifier: MIT

from datetime import datetime, timedelta
from uuid import UUID
from pydantic import (
    BaseModel,
    FiniteFloat,
    ValidationInfo,
    field_validator,
    model_validator,
)

ALLOWED_SERIES = {"eeg", "wholesale", "community", "home"}
SERIES_NAMES = ("eeg", "wholesale", "community", "home")
TIME_STEP = timedelta(minutes=15)


def _check_trade_ids(value: list[UUID]) -> list[UUID]:
    if not value:
        raise ValueError("trade_ids must contain at least one trade.")
    if len(set(value)) != len(value):
        raise ValueError("trade_ids must not contain duplicates.")
    return value


class TimeseriesPayload(BaseModel):
    # identifies the buyer; the schedule is assigned to the sellers it was matched with
    blockchain_address: str
    # trades of the market clearing the schedule is made up of; their sellers share it
    trade_ids: list[UUID]
    time_steps: list[datetime]
    eeg: None | list[FiniteFloat] = None
    wholesale: None | list[FiniteFloat] = None
    community: None | list[FiniteFloat] = None
    home: None | list[FiniteFloat] = None

    _validate_trade_ids = field_validator("trade_ids")(_check_trade_ids)

    @field_validator("time_steps")
    @classmethod
    def _check_time_steps_sorted(cls, value: list[datetime]) -> list[datetime]:
        """Run after parsing, so the comparison is on real datetimes rather than on the
        raw strings — otherwise ordering would be decided lexicographically and mixing
        tz-aware with tz-naive values would go unnoticed."""
        for prev, cur in zip(value, value[1:]):
            try:
                if cur <= prev:
                    raise ValueError(
                        "time_steps must be strictly increasing (sorted ascending, no duplicates)."
                    )
                if cur - prev != TIME_STEP:
                    raise ValueError("time_steps must be exactly 15 minutes apart.")
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

    @model_validator(mode="after")
    def _check_at_least_one_series(self) -> "TimeseriesPayload":
        if all(getattr(self, name) is None for name in SERIES_NAMES):
            raise ValueError(
                f"At least one series must be present ({', '.join(SERIES_NAMES)})."
            )
        return self


class PowerRequestResponse(BaseModel):
    """Identifier of the accepted power request.

    The fulfiller receives the same ``request_id`` via MQTT and echoes it back in
    :class:`ActualSchedulePayload`, so the requester needs it to correlate the two.
    """

    request_id: str


class ActualSchedulePayload(BaseModel):
    """Schedule actually driven by the fulfiller, sent back for validation."""

    request_id: str
    # identifies the seller; its share of the request is validated
    blockchain_address: str
    # trades of this seller the request was assigned from
    trade_ids: list[UUID]
    time_steps: list[str]
    power_rate_w: list[FiniteFloat]

    _validate_trade_ids = field_validator("trade_ids")(_check_trade_ids)

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


class MarketResultMatch(BaseModel):
    """Counterparty an accepted order was matched with in the market clearing."""

    # identical for buyer and seller of the same trade
    trade_id: UUID
    bid_id: str
    blockchain_address: str
    volume: FiniteFloat
    price: FiniteFloat


class MarketResultOrder(BaseModel):
    """One accepted order of the market, with the counterparties it trades with."""

    bid_id: str
    unit_id: str
    blockchain_address: str
    start_time: datetime
    end_time: datetime
    accepted_volume: FiniteFloat
    accepted_price: FiniteFloat
    matches: list[MarketResultMatch]


class MarketResultsPayload(BaseModel):
    """Accepted orders of one market clearing, sent by ASSUME."""

    market_id: str
    # delivery period of the cleared product, also sent if no order was accepted
    start_time: datetime
    end_time: datetime
    orders: list[MarketResultOrder]
