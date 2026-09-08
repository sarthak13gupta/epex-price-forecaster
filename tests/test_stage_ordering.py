"""
The exogenous cascade's stage ordering is a *correctness* requirement, not a
style preference: HDD/CDD are derived from T_lisse, which is itself a stage-2
output. Building degree days before the national-temperature stage has run was
the original migration defect — it produced a hard KeyError, but a future
refactor could just as easily produce silently wrong columns. These tests pin
the guard and the column ordering that the persisted artifact depends on.
"""
import pandas as pd
import pytest

from src.features.build_features import engineer_degree_days, engineer_national_temperature
from src.utils.config_loader import get_feature_names, get_hdd_cdd_columns


def test_degree_days_before_national_temperature_raises(synthetic_processed, config):
    """Degree days from T_lisse must fail loudly if stage 2 has not run."""
    reference = config["feature_engineering"]["hdd_cdd_reference"]
    assert "T_lisse" in reference, "this test assumes the T_lisse reference"

    with pytest.raises(KeyError, match="engineer_national_temperature must run first"):
        engineer_degree_days(synthetic_processed, reference, config)


def test_degree_days_after_national_temperature_succeeds(synthetic_processed, config):
    """The correct ordering produces the HDD/CDD columns the model consumes."""
    enriched, _ = engineer_national_temperature(synthetic_processed, config)
    enriched = engineer_degree_days(
        enriched, config["feature_engineering"]["hdd_cdd_reference"], config
    )

    for column in get_hdd_cdd_columns(config):
        assert column in enriched.columns
        assert (enriched[column] >= 0).all(), f"{column} must be non-negative"


def test_degree_days_are_complementary(synthetic_processed, config):
    """
    A day is either heating or cooling, never both: the thresholds are 15 and
    22 C, so between them both series are zero (the 'dead zone').
    """
    enriched, _ = engineer_national_temperature(synthetic_processed, config)
    enriched = engineer_degree_days(
        enriched, config["feature_engineering"]["hdd_cdd_reference"], config
    )

    both_active = (enriched["T_lisse_HDD"] > 0) & (enriched["T_lisse_CDD"] > 0)
    assert not both_active.any()


def test_feature_name_ordering_is_stable(config):
    """
    The design-matrix column order is baked into the model artifact — the API
    must present columns in exactly this order. Guard against silent reordering.
    """
    names = get_feature_names(config)

    assert len(names) == len(set(names)), "duplicate feature names"
    assert names[: len(config["features"]["calendar"])] == list(config["features"]["calendar"])
    for column in get_hdd_cdd_columns(config):
        assert column in names
    # Under the notebook feature set the raw levels come last, after the
    # engineered blocks.
    if config["features"]["feature_set"] == "notebook":
        assert names[-2:] == [
            config["feature_engineering"]["nuclear_col"],
            config["feature_engineering"]["demand_col"],
        ]


def test_unknown_feature_set_is_rejected(config):
    bad = {**config, "features": {**config["features"], "feature_set": "nope"}}
    with pytest.raises(ValueError, match="Unknown features.feature_set"):
        get_feature_names(bad)
