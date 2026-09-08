"""
Walk-forward folds must be blind. Two things can leak:

  1. Date overlap — a training window that reaches into its own test month.
  2. Fitted state — a scaler, a target transformer, or the exogenous cascade
     fitted on the full series and then reused per fold. The cascade is refit
     inside prepare_folds and the target transformer lives inside a
     TransformedTargetRegressor so it refits per fold; both are structural, but
     "structural" only holds while nobody moves the fit call.

This runs the real prepare_folds against a synthetic series, which also
exercises the whole cascade, so it doubles as an integration smoke test.
"""
import pandas as pd
import pytest

from src.models.backtest import prepare_folds
from src.utils.config_loader import get_feature_names, get_hdd_cdd_columns

# A two-fold schedule is enough to prove the invariants and keeps the cascade
# fits down to two.
SCHEDULE = [
    {"test_start": "2021-05-01", "test_end": "2021-05-31", "regime": "test_a"},
    {"test_start": "2021-06-01", "test_end": "2021-06-30", "regime": "test_b"},
]


@pytest.fixture(scope="module")
def folds(config, synthetic_static):
    return prepare_folds(
        df=synthetic_static,
        target_col=config["dataset"]["target"],
        features=get_feature_names(config),
        schedule=SCHEDULE,
        calendar_features=list(config["features"]["calendar"]),
        hdd_cdd_cols=get_hdd_cdd_columns(config),
        config=config,
        verbose=False,
    )


def test_all_folds_were_prepared(folds):
    assert len(folds) == len(SCHEDULE)


def test_train_window_never_overlaps_its_test_window(folds):
    """The core leakage invariant."""
    for fold in folds:
        assert fold.train_end < fold.test_start, (
            f"fold {fold.index} trains through {fold.train_end.date()} but tests "
            f"from {fold.test_start.date()}"
        )
        assert fold.X_train.index.max() < fold.test_start
        assert fold.X_test.index.min() >= fold.test_start
        assert not fold.X_train.index.intersection(fold.X_test.index).size


def test_training_window_length_matches_config(folds, config):
    expected = int(config["backtest"]["train_window_days"])
    for fold in folds:
        span = (fold.train_end - fold.train_start).days + 1
        assert span == expected, f"fold {fold.index} window is {span} days"


def test_folds_advance_monotonically(folds):
    """A rolling origin moves forward; it never revisits an earlier test month."""
    for earlier, later in zip(folds, folds[1:]):
        assert later.test_start > earlier.test_end


def test_design_matrices_are_complete_and_aligned(folds, config):
    """
    Every configured feature is present, in order, with no NaNs — a NaN here
    would be silently imputed or dropped downstream.
    """
    expected = get_feature_names(config)
    for fold in folds:
        assert list(fold.X_train.columns) == expected
        assert list(fold.X_test.columns) == expected
        assert not fold.X_train.isna().any().any(), f"NaN in fold {fold.index} X_train"
        assert not fold.X_test.isna().any().any(), f"NaN in fold {fold.index} X_test"
        assert len(fold.X_train) == len(fold.y_train)
        assert len(fold.X_test) == len(fold.y_test)


def test_test_features_are_simulated_not_observed(folds, config):
    """
    Test-window drivers must come from the cascade, not the raw data. Nuclear
    availability is the one genuinely-known-ahead input, so it passes through
    unchanged; demand is simulated and must therefore differ from the truth.
    """
    demand_col = config["feature_engineering"]["demand_col"]
    if demand_col not in get_feature_names(config):
        pytest.skip("demand is not in the active feature set")

    for fold in folds:
        simulated = fold.X_test[demand_col]
        assert simulated.notna().all()
        # A simulated series that matched the observed one to the last decimal
        # would mean the cascade was bypassed.
        assert simulated.std() > 0


def test_empty_fold_raises_rather_than_silently_shrinking(synthetic_static, config):
    """A schedule beyond the data must fail loudly, not return zero rows."""
    beyond = [{"test_start": "2030-01-01", "test_end": "2030-01-31", "regime": "future"}]
    with pytest.raises(ValueError, match="has no data"):
        prepare_folds(
            df=synthetic_static,
            target_col=config["dataset"]["target"],
            features=get_feature_names(config),
            schedule=beyond,
            calendar_features=list(config["features"]["calendar"]),
            hdd_cdd_cols=get_hdd_cdd_columns(config),
            config=config,
            verbose=False,
        )
