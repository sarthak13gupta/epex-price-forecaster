"""
The model registry: every candidate the training pipeline benchmarks.

Each entry pairs an unfitted estimator with an optional Optuna search space, so
adding a new algorithm (a neural network, a gradient-boosted alternative) is a
matter of appending one spec here and naming it in `models.enabled`.
"""

# Deferred annotations so the optuna type references below stay strings and
# need no import at runtime.
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

import numpy as np
import numpy.typing as npt
import pandas as pd
from sklearn.base import BaseEstimator, RegressorMixin
from sklearn.compose import TransformedTargetRegressor
from sklearn.linear_model import ElasticNet
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBRegressor

from src.models.simulate_exogenous import RobustArcSinTransformer

if TYPE_CHECKING:  # pragma: no cover
    # optuna is a TRAINING dependency and is absent from the slim serving
    # environment, but this module is on the serving path: NaiveForecaster
    # lives here, so unpickling a Baseline_Seasonal champion imports it. The
    # search spaces are only ever called during tuning, where optuna is
    # installed, so the reference can stay type-only.
    import optuna

ParamSpace = Callable[["optuna.trial.Trial"], dict[str, Any]]


class NaiveForecaster(BaseEstimator, RegressorMixin):
    """
    Unified baseline forecaster. The honesty check every other model must beat.

    Strategies:
      - 'last':     predicts the last observed value
      - 'mean':     predicts the training mean
      - 'median':   predicts the training median
      - 'seasonal': predicts the value from exactly `shift_days` ago
    """

    def __init__(self, strategy: str = 'last', shift_days: int = 364) -> None:
        self.strategy = strategy
        self.shift_days = shift_days

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "NaiveForecaster":
        if self.strategy == 'last':
            self.learned_value_ = float(y.iloc[-1])
        elif self.strategy == 'mean':
            self.learned_value_ = float(y.mean())
        elif self.strategy == 'median':
            self.learned_value_ = float(y.median())
        elif self.strategy == 'seasonal':
            self.history_y_ = y.copy()
        else:
            raise ValueError(
                f"Unknown strategy {self.strategy!r}. Expected one of "
                "'last', 'mean', 'median', 'seasonal'."
            )
        return self

    def predict(self, X: pd.DataFrame) -> npt.NDArray[np.float64]:
        if self.strategy in ('last', 'mean', 'median'):
            return np.full(
                shape=(len(X),), fill_value=self.learned_value_, dtype=np.float64
            )

        # Seasonal: reach back `shift_days` from each requested date. 364 keeps
        # the day-of-week aligned, which matters for a market with a strong
        # weekday/weekend split.
        target_dates = pd.DatetimeIndex(X.index) - pd.Timedelta(self.shift_days, unit="D")
        predictions = self.history_y_.reindex(target_dates, method='nearest')
        return predictions.to_numpy(dtype=np.float64)


@dataclass
class ModelSpec:
    """One benchmark candidate."""

    name: str
    estimator: Any
    requires_tuning: bool
    param_space: ParamSpace | None = None


def _build_elastic_net(config: dict[str, Any]) -> tuple[Any, ParamSpace]:
    """Regularised linear model on the arcsinh-transformed target."""
    models = config["models"]
    bounds = models["elastic_net"]
    random_state = int(models["random_state"])

    pipeline = Pipeline([
        ('scaler', StandardScaler()),
        ('model', ElasticNet(
            random_state=random_state, max_iter=int(bounds["max_iter"])
        )),
    ])

    estimator = TransformedTargetRegressor(
        regressor=pipeline, transformer=RobustArcSinTransformer()
    )

    def param_space(trial: optuna.trial.Trial) -> dict[str, Any]:
        return {
            'regressor__model__alpha': trial.suggest_float(
                'regressor__model__alpha',
                float(bounds["alpha_min"]),
                float(bounds["alpha_max"]),
                log=True,
            ),
            'regressor__model__l1_ratio': trial.suggest_float(
                'regressor__model__l1_ratio',
                float(bounds["l1_ratio_min"]),
                float(bounds["l1_ratio_max"]),
            ),
        }

    return estimator, param_space


def _build_xgboost(config: dict[str, Any]) -> tuple[Any, ParamSpace]:
    """Gradient-boosted trees on the arcsinh-transformed target."""
    models = config["models"]
    bounds = models["xgboost"]
    random_state = int(models["random_state"])

    pipeline = Pipeline([
        ('scaler', StandardScaler()),
        ('model', XGBRegressor(
            random_state=random_state, n_jobs=-1, tree_method='hist'
        )),
    ])

    estimator = TransformedTargetRegressor(
        regressor=pipeline, transformer=RobustArcSinTransformer()
    )

    def param_space(trial: optuna.trial.Trial) -> dict[str, Any]:
        return {
            'regressor__model__max_depth': trial.suggest_int(
                'regressor__model__max_depth',
                int(bounds["max_depth_min"]),
                int(bounds["max_depth_max"]),
            ),
            'regressor__model__n_estimators': trial.suggest_int(
                'regressor__model__n_estimators',
                int(bounds["n_estimators_min"]),
                int(bounds["n_estimators_max"]),
                step=int(bounds["n_estimators_step"]),
            ),
            'regressor__model__learning_rate': trial.suggest_float(
                'regressor__model__learning_rate',
                float(bounds["learning_rate_min"]),
                float(bounds["learning_rate_max"]),
                log=True,
            ),
            'regressor__model__subsample': trial.suggest_float(
                'regressor__model__subsample',
                float(bounds["subsample_min"]),
                float(bounds["subsample_max"]),
            ),
            'regressor__model__colsample_bytree': trial.suggest_float(
                'regressor__model__colsample_bytree',
                float(bounds["colsample_bytree_min"]),
                float(bounds["colsample_bytree_max"]),
            ),
        }

    return estimator, param_space


def _build_baseline_seasonal(config: dict[str, Any]) -> Any:
    """Same day-of-week, one year back. No tuning, no features."""
    shift_days = int(config["models"]["baseline_seasonal"]["shift_days"])
    return NaiveForecaster(strategy='seasonal', shift_days=shift_days)


def build_model_registry(config: dict[str, Any]) -> dict[str, ModelSpec]:
    """
    Assembles the specs named in `models.enabled`, preserving config order.

    Raises on an unknown name rather than silently skipping it, so a typo in
    config fails the run instead of quietly shrinking the benchmark.
    """
    elastic_net_estimator, elastic_net_space = _build_elastic_net(config)
    xgboost_estimator, xgboost_space = _build_xgboost(config)

    available: dict[str, ModelSpec] = {
        "Baseline_Seasonal": ModelSpec(
            name="Baseline_Seasonal",
            estimator=_build_baseline_seasonal(config),
            requires_tuning=False,
        ),
        "ElasticNet": ModelSpec(
            name="ElasticNet",
            estimator=elastic_net_estimator,
            requires_tuning=True,
            param_space=elastic_net_space,
        ),
        "XGBoost": ModelSpec(
            name="XGBoost",
            estimator=xgboost_estimator,
            requires_tuning=True,
            param_space=xgboost_space,
        ),
    }

    enabled = config["models"]["enabled"]
    unknown = [name for name in enabled if name not in available]
    if unknown:
        raise ValueError(
            f"Unknown model name(s) in models.enabled: {unknown}. "
            f"Available: {sorted(available)}"
        )

    return {name: available[name] for name in enabled}
