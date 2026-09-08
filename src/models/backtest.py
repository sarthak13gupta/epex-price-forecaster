"""
Walk-forward temporal cross-validation with exogenous simulation.

Fold preparation and fold evaluation are split on purpose. The exogenous
cascade (weather OLS -> thermal -> Ridge load model) depends only on the
training window, never on the price model's hyperparameters, so it is fitted
once per fold and reused across every Optuna trial. Refitting it per trial —
as the notebook does — multiplies the tuning cost by the number of trials for
identical results.
"""

from dataclasses import dataclass
from typing import Any, Protocol, cast

import pandas as pd
from pandera.typing import DataFrame
from sklearn.base import clone
from sklearn.metrics import mean_absolute_error, root_mean_squared_error

from src.data.schema import ProcessedDataSchema
from src.models.simulate_exogenous import ExogenousCascade


class ScikitLearnModel(Protocol):
    """The estimator surface this project relies on, structurally typed."""

    def fit(self, X: pd.DataFrame, y: pd.Series) -> Any: ...
    def predict(self, X: pd.DataFrame) -> Any: ...
    def get_params(self, deep: bool = True) -> dict[str, Any]: ...
    def set_params(self, **params: Any) -> Any: ...


@dataclass
class PreparedFold:
    """One walk-forward fold with its exogenous inputs already simulated."""

    index: int
    regime: str
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    X_train: pd.DataFrame
    y_train: pd.Series
    X_test: pd.DataFrame
    y_test: pd.Series


def prepare_folds(
    df: DataFrame[ProcessedDataSchema],
    target_col: str,
    features: list[str],
    schedule: list[dict[str, Any]],
    calendar_features: list[str],
    hdd_cdd_cols: list[str],
    config: dict[str, Any],
    verbose: bool = True,
) -> list[PreparedFold]:
    """
    Builds every fold's design matrices, refitting the exogenous cascade on each
    rolling training window so the test inputs stay as blind as production.
    """
    train_window_days = int(config["backtest"]["train_window_days"])
    folds: list[PreparedFold] = []

    if verbose:
        print(f"Preparing {len(schedule)} walk-forward folds...")

    for idx, fold in enumerate(schedule, 1):
        # Force string casting for timestamp parsing to satisfy strict typing
        test_start = pd.Timestamp(str(fold['test_start']))
        test_end = pd.Timestamp(str(fold['test_end']))

        train_start = test_start - pd.Timedelta(train_window_days, unit="D")
        train_end = test_start - pd.Timedelta(1, unit="D")

        # Pandas .loc returns a generic pd.DataFrame, so cast it back to the
        # strict schema contract the cascade is typed against.
        train_data = cast(
            DataFrame[ProcessedDataSchema], df.loc[train_start:train_end].copy()
        )
        test_data = df.loc[test_start:test_end].copy()

        if train_data.empty or test_data.empty:
            raise ValueError(
                f"Fold {idx} ({fold['regime']}) has no data: "
                f"train {train_start.date()}..{train_end.date()} -> "
                f"{len(train_data)} rows, "
                f"test {test_start.date()}..{test_end.date()} -> "
                f"{len(test_data)} rows."
            )

        cascade = ExogenousCascade(
            config=config,
            calendar_features=calendar_features,
            hdd_cdd_columns=hdd_cdd_cols,
        ).fit(train_data)

        enriched_train = cascade.enriched_train
        simulated_test = cascade.simulate(test_data)

        folds.append(PreparedFold(
            index=idx,
            regime=str(fold['regime']),
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            X_train=enriched_train[features],
            y_train=enriched_train[target_col],
            X_test=simulated_test[features],
            y_test=simulated_test[target_col],
        ))

        if verbose:
            print(
                f"  Fold {idx:02d} ({fold['regime']}): "
                f"{len(enriched_train)} train / {len(simulated_test)} test days"
            )

    return folds


def evaluate_folds(
    folds: list[PreparedFold],
    model: ScikitLearnModel,
    model_name: str,
    verbose: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Fits and scores `model` on each prepared fold.

    Each fold gets a fresh clone so no state leaks between folds.
    """
    all_fold_metrics = []
    all_fold_preds = []

    for fold in folds:
        fold_model = cast(ScikitLearnModel, clone(cast(Any, model)))
        fold_model.fit(fold.X_train, fold.y_train)
        preds = fold_model.predict(fold.X_test)

        mae = float(mean_absolute_error(fold.y_test, preds))
        rmse = float(root_mean_squared_error(fold.y_test, preds))

        all_fold_metrics.append({
            'model': model_name, 'fold': fold.index, 'regime': fold.regime,
            'train_start': fold.train_start, 'train_end': fold.train_end,
            'test_start': fold.test_start, 'test_end': fold.test_end,
            'n_test_days': len(fold.y_test),
            'mae': mae, 'rmse': rmse,
        })

        all_fold_preds.append(pd.DataFrame({
            "model": model_name,
            "actual_price": fold.y_test,
            "predicted_price": preds,
            "regime": fold.regime,
            "fold": fold.index,
        }, index=fold.X_test.index))

        if verbose:
            print(
                f"Fold {fold.index:02d} ({fold.regime}): "
                f"MAE = {mae:.2f} EUR/MWh | RMSE = {rmse:.2f} EUR/MWh"
            )

    metrics_df = pd.DataFrame(all_fold_metrics)

    if verbose:
        print("=" * 60)
        print(
            f"{model_name}: mean MAE = {metrics_df['mae'].mean():.2f} | "
            f"mean RMSE = {metrics_df['rmse'].mean():.2f} EUR/MWh"
        )

    return metrics_df, pd.concat(all_fold_preds)


def run_backtest(
    df: DataFrame[ProcessedDataSchema],
    target_col: str,
    features: list[str],
    model: ScikitLearnModel,
    model_name: str,
    schedule: list[dict[str, Any]],
    calendar_features: list[str],
    hdd_cdd_cols: list[str],
    config: dict[str, Any],
    verbose: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Prepares folds and evaluates one model over them, end to end.

    Convenience wrapper for one-off runs. The training pipeline prepares folds
    itself and reuses them across models and tuning trials.
    """
    folds = prepare_folds(
        df=df,
        target_col=target_col,
        features=features,
        schedule=schedule,
        calendar_features=calendar_features,
        hdd_cdd_cols=hdd_cdd_cols,
        config=config,
        verbose=verbose,
    )
    return evaluate_folds(folds, model, model_name, verbose=verbose)
