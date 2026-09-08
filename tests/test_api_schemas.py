"""
Request validation is the API's outermost guard, and the failure it exists to
catch is a *unit* error, not a missing field. Nuclear availability arrives in
MW; the same number in GW (29 instead of 29,000) passes every null and type
check, then drives residual demand deeply negative and produces a confident
nonsense price. These tests pin the plausible band and the NaN rejection.
"""
import math

import pytest
from pydantic import ValidationError

from src.api.schemas import (
    NUCLEAR_MAX_MW,
    NUCLEAR_MIN_MW,
    ForecastRequest,
)


def test_valid_request_is_accepted():
    request = ForecastRequest(start_date="2020-07-01", nuclear_avail=[29049.0, 29466.0])
    assert len(request.nuclear_avail) == 2
    assert str(request.start_date) == "2020-07-01"


def test_horizon_is_implied_by_the_list_length():
    """There is no separate horizon field to disagree with the payload."""
    assert "horizon" not in ForecastRequest.model_fields
    request = ForecastRequest(start_date="2020-07-01", nuclear_avail=[30000.0] * 31)
    assert len(request.nuclear_avail) == 31


def test_gigawatt_unit_error_is_rejected():
    """The realistic mistake: 29 GW submitted as 29."""
    with pytest.raises(ValidationError, match="not GW"):
        ForecastRequest(start_date="2020-07-01", nuclear_avail=[29.0, 30.0])


def test_nan_is_rejected():
    with pytest.raises(ValidationError, match="is NaN"):
        ForecastRequest(start_date="2020-07-01", nuclear_avail=[30000.0, math.nan])


def test_negative_availability_is_rejected():
    with pytest.raises(ValidationError, match="outside the plausible range"):
        ForecastRequest(start_date="2020-07-01", nuclear_avail=[-1000.0])


def test_absurdly_large_availability_is_rejected():
    """More nuclear than France has ever had installed."""
    with pytest.raises(ValidationError, match="outside the plausible range"):
        ForecastRequest(start_date="2020-07-01", nuclear_avail=[NUCLEAR_MAX_MW + 1.0])


def test_empty_horizon_is_rejected():
    with pytest.raises(ValidationError):
        ForecastRequest(start_date="2020-07-01", nuclear_avail=[])


def test_band_boundaries_are_inclusive():
    """The guard rejects what is outside the band, not what sits on its edge."""
    request = ForecastRequest(
        start_date="2020-07-01", nuclear_avail=[NUCLEAR_MIN_MW, NUCLEAR_MAX_MW]
    )
    assert request.nuclear_avail == [NUCLEAR_MIN_MW, NUCLEAR_MAX_MW]


def test_the_error_message_names_the_offending_index():
    """A 31-day payload needs to say *which* day is wrong."""
    with pytest.raises(ValidationError, match=r"nuclear_avail\[2\]"):
        ForecastRequest(
            start_date="2020-07-01", nuclear_avail=[30000.0, 30000.0, 29.0]
        )


def test_malformed_date_is_rejected():
    with pytest.raises(ValidationError):
        ForecastRequest(start_date="not-a-date", nuclear_avail=[30000.0])
