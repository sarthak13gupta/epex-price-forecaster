"""
The horizon guards are the API's contract with reality. The weather stage is a
Fourier trend plus AR(2); the AR term decays to the deterministic seasonal mean
within a couple of weeks, so a 90-day "forecast" would be climatology wearing a
forecast's clothes. And a request that starts before train_end + 1 is asking
the model to predict days it was fitted on.

These construct a PriceForecaster directly with dummy stages — _validate_horizon
reads only the metadata, so no fitting is needed and the tests stay fast.
"""
import pandas as pd
import pytest

from src.models.forecaster import ForecasterMetadata, PriceForecaster

TRAIN_END = "2020-06-30"
MAX_HORIZON = 31


@pytest.fixture
def forecaster(config) -> PriceForecaster:
    metadata = ForecasterMetadata(
        model_name="ElasticNet",
        target_col=config["dataset"]["target"],
        feature_names=["a", "b"],
        train_start="2018-06-30",
        train_end=TRAIN_END,
        n_train_days=731,
        max_horizon_days=MAX_HORIZON,
    )
    # The cascade and price model are never touched by the horizon guard.
    return PriceForecaster(
        cascade=None,          # type: ignore[arg-type]
        price_model=None,      # type: ignore[arg-type]
        config=config,
        feature_names=["a", "b"],
        metadata=metadata,
    )


def _index(start: str, days: int) -> pd.DatetimeIndex:
    return pd.date_range(start, periods=days, freq="D")


def test_earliest_forecast_date_is_day_after_train_end(forecaster):
    assert forecaster.earliest_forecast_date == pd.Timestamp("2020-07-01")


def test_horizon_at_the_limit_is_accepted(forecaster):
    forecaster._validate_horizon(_index("2020-07-01", MAX_HORIZON))


def test_horizon_beyond_the_limit_is_rejected(forecaster):
    with pytest.raises(ValueError, match="exceeds the configured"):
        forecaster._validate_horizon(_index("2020-07-01", MAX_HORIZON + 1))


def test_forecast_starting_inside_the_training_window_is_rejected(forecaster):
    """Asking for a day the model was fitted on is in-sample, not a forecast."""
    with pytest.raises(ValueError, match="earliest forecastable"):
        forecaster._validate_horizon(_index("2020-06-15", 10))


def test_forecast_starting_on_train_end_is_rejected(forecaster):
    """Off-by-one boundary: train_end itself is still history."""
    with pytest.raises(ValueError, match="earliest forecastable"):
        forecaster._validate_horizon(_index(TRAIN_END, 5))


def test_gap_after_train_end_is_allowed(forecaster):
    """Starting later than the earliest date is fine — the cascade extrapolates."""
    forecaster._validate_horizon(_index("2020-07-10", 5))


def test_configured_max_horizon_matches_the_forecast_config(config):
    """The guard's limit must come from config, not a hardcoded literal."""
    assert config["forecast"]["max_horizon_days"] == MAX_HORIZON
