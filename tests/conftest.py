"""
Shared fixtures.

The tests here are deliberately cheap: they assert the *structural* invariants
that silently produce wrong numbers when broken (stage ordering, horizon
bounds, fold disjointness), not model accuracy. Accuracy is what the
walk-forward backtest is for, and it needs the real dataset.
"""
import numpy as np
import pandas as pd
import pytest

from src.utils.config_loader import load_config


@pytest.fixture(scope="session")
def config() -> dict:
    """The real project config — these invariants are config-driven."""
    return load_config()


@pytest.fixture(scope="session")
def synthetic_processed(config) -> pd.DataFrame:
    """
    A processed-shaped frame with a seasonal price and plausible drivers.

    Three years so a 731-day rolling training window plus a test month fits.
    Values are synthetic; only the schema and the date index matter. Session
    scoped and treated as read-only — every consumer in src/ copies before
    mutating, so the fixture is built once.
    """
    fe = config["feature_engineering"]
    index = pd.date_range("2019-01-01", periods=3 * 365, freq="D", name="Date")
    day_of_year = index.dayofyear.to_numpy()
    seasonal = np.sin(2 * np.pi * day_of_year / 365.25)
    rng = np.random.default_rng(0)

    frame = pd.DataFrame(index=index)
    frame[config["dataset"]["target"]] = 45.0 + 15.0 * seasonal + rng.normal(0, 5, len(index))
    frame[fe["demand_col"]] = 55_000.0 - 12_000.0 * seasonal + rng.normal(0, 1_500, len(index))
    frame[fe["nuclear_col"]] = 45_000.0 - 8_000.0 * seasonal + rng.normal(0, 1_000, len(index))

    for city in config["features"]["weather"]:
        frame[city] = 13.0 + 9.0 * seasonal + rng.normal(0, 2, len(index))

    return frame


@pytest.fixture(scope="session")
def synthetic_static(synthetic_processed, config) -> pd.DataFrame:
    """
    The synthetic frame after the fold-independent feature stage.

    This mirrors what the training pipeline hands to prepare_folds: calendar
    flags and residual-demand polynomials are already present, while the
    thermal features are not, because those depend on a training window and
    are built inside the cascade.
    """
    from src.features.build_features import build_static_features

    return build_static_features(synthetic_processed, config)  # type: ignore[arg-type]
