"""
The served artifact.

The notebook refits the price model on every forecast call. That is fine in a
notebook and unacceptable in an API, so this module bundles everything a
forecast needs into one fitted object:

  - the fitted ExogenousCascade (weather coefficients, seasonal temperature
    norm, Ridge load model, and the warm-up tail of real history)
  - the fitted price model, including its arcsinh target transform
  - the exact feature ordering the price model was trained on
  - the training window bounds, so the API can reject horizons it cannot serve

`PriceForecaster` is the plain-Python object; `PriceForecasterModel` wraps it
for MLflow so the same artifact can be loaded by `mlflow.pyfunc.load_model`.
"""

from dataclasses import dataclass, field
from typing import Any, cast

import mlflow.pyfunc
import pandas as pd
from pandera.typing import DataFrame
from sklearn.base import clone

from src.data.schema import FutureExogenousSchema, ProcessedDataSchema
from src.features.build_features import engineer_calendar_features
from src.models.backtest import ScikitLearnModel
from src.models.simulate_exogenous import ExogenousCascade

# Columns surfaced alongside the prediction so a user can see what the cascade
# assumed, rather than just the price it produced.
DIAGNOSTIC_COLUMNS = (
    "T_lisse",
    "Delta_T",
    "T_lisse_HDD",
    "T_lisse_CDD",
    "Residual_Demand",
)


@dataclass
class ForecasterMetadata:
    """Provenance for a fitted forecaster, surfaced by the API's /model-info."""

    model_name: str
    target_col: str
    feature_names: list[str]
    train_start: str
    train_end: str
    n_train_days: int
    max_horizon_days: int
    backtest_mae: float | None = None
    backtest_rmse: float | None = None
    best_params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "target_col": self.target_col,
            "feature_names": list(self.feature_names),
            "n_features": len(self.feature_names),
            "train_start": self.train_start,
            "train_end": self.train_end,
            "n_train_days": self.n_train_days,
            "max_horizon_days": self.max_horizon_days,
            "backtest_mae": self.backtest_mae,
            "backtest_rmse": self.backtest_rmse,
            "best_params": dict(self.best_params),
        }


class PriceForecaster:
    """A fitted cascade plus a fitted price model, ready to forecast."""

    def __init__(
        self,
        cascade: ExogenousCascade,
        price_model: ScikitLearnModel,
        config: dict[str, Any],
        feature_names: list[str],
        metadata: ForecasterMetadata,
    ) -> None:
        self.cascade = cascade
        self.price_model = price_model
        self.config = config
        self.feature_names = feature_names
        self.metadata = metadata

    # ------------------------------------------------------------------ build

    @classmethod
    def fit(
        cls,
        train_data: DataFrame[ProcessedDataSchema],
        price_model: ScikitLearnModel,
        model_name: str,
        config: dict[str, Any],
        feature_names: list[str],
        calendar_features: list[str],
        hdd_cdd_cols: list[str],
        backtest_mae: float | None = None,
        backtest_rmse: float | None = None,
        best_params: dict[str, Any] | None = None,
    ) -> "PriceForecaster":
        """
        Fits the cascade and the price model on one training window.

        The price model is cloned first so the tuned-but-unfitted estimator
        handed in by the registry stays reusable.
        """
        target_col = config["dataset"]["target"]

        cascade = ExogenousCascade(
            config=config,
            calendar_features=calendar_features,
            hdd_cdd_columns=hdd_cdd_cols,
        ).fit(train_data)

        enriched_train = cascade.enriched_train

        fitted_model = cast(ScikitLearnModel, clone(cast(Any, price_model)))
        fitted_model.fit(
            enriched_train[feature_names], enriched_train[target_col]
        )

        metadata = ForecasterMetadata(
            model_name=model_name,
            target_col=target_col,
            feature_names=list(feature_names),
            train_start=str(pd.Timestamp(enriched_train.index[0]).date()),
            train_end=str(pd.Timestamp(enriched_train.index[-1]).date()),
            n_train_days=len(enriched_train),
            max_horizon_days=int(config["forecast"]["max_horizon_days"]),
            backtest_mae=backtest_mae,
            backtest_rmse=backtest_rmse,
            # Prefer the tuned parameters from the search. get_params on a
            # TransformedTargetRegressor only exposes wrapper-level arguments,
            # so falling back to it would report nothing useful.
            best_params=dict(best_params) if best_params else {
                key: value
                for key, value in fitted_model.get_params(deep=True).items()
                if isinstance(value, (int, float, str, bool))
                and key.startswith("regressor__model__")
            },
        )

        return cls(
            cascade=cascade,
            price_model=fitted_model,
            config=config,
            feature_names=list(feature_names),
            metadata=metadata,
        )

    # --------------------------------------------------------------- forecast

    @property
    def train_end(self) -> pd.Timestamp:
        """Last day of observed history the forecaster was fitted on."""
        return pd.Timestamp(self.metadata.train_end)

    @property
    def earliest_forecast_date(self) -> pd.Timestamp:
        """First date this forecaster can produce a value for."""
        return self.train_end + pd.Timedelta(1, unit="D")

    def _validate_horizon(self, index: pd.DatetimeIndex) -> None:
        max_horizon = self.metadata.max_horizon_days

        if len(index) > max_horizon:
            raise ValueError(
                f"Requested horizon of {len(index)} days exceeds the configured "
                f"maximum of {max_horizon}. The weather model's AR(2) component "
                "decays to its deterministic seasonal mean well before that, so "
                "longer horizons would be misleading."
            )

        if index[0] < self.earliest_forecast_date:
            raise ValueError(
                f"Forecast starts {index[0].date()} but the model was trained "
                f"through {self.train_end.date()}. The earliest forecastable "
                f"date is {self.earliest_forecast_date.date()}."
            )

    def forecast(self, future_exog: pd.DataFrame) -> pd.DataFrame:
        """
        Produces a price path over the dates in `future_exog`.

        `future_exog` needs a daily DatetimeIndex (or a Date column) and the
        known-ahead nuclear availability. Temperature and demand columns, if
        present, are overwritten by the cascade.

        Returns the prediction plus the exogenous state the cascade assumed, so
        a forecast can be interrogated rather than merely trusted.
        """
        frame = future_exog.copy()

        date_col = self.config["dataset"]["date_col"]
        if date_col in frame.columns:
            frame[date_col] = pd.to_datetime(frame[date_col])
            frame = frame.set_index(date_col)
        frame.index = pd.to_datetime(frame.index)
        frame = frame.sort_index()
        frame.index.name = "Date"

        validated = FutureExogenousSchema.validate(frame)
        index = pd.DatetimeIndex(validated.index)
        self._validate_horizon(index)

        # Calendar features depend only on the index, so they are built here
        # rather than being required of the caller.
        with_calendar = engineer_calendar_features(
            cast(pd.DataFrame, validated), self.config
        )

        simulated = self.cascade.simulate(with_calendar)
        predictions = self.price_model.predict(simulated[self.feature_names])

        result = pd.DataFrame(
            {"predicted_price": predictions}, index=simulated.index
        )
        result.index.name = "Date"

        # Carry through the simulated drivers and the actuals when backtesting
        # against a period where they happen to be known.
        demand_col = self.config["feature_engineering"]["demand_col"]
        nuclear_col = self.config["feature_engineering"]["nuclear_col"]

        for column in (*DIAGNOSTIC_COLUMNS, demand_col, nuclear_col):
            if column in simulated.columns:
                result[column] = simulated[column]

        # When forecasting over a historical period the actuals are present;
        # carry them through so the caller can score the forecast directly.
        target_col = self.config["dataset"]["target"]
        if target_col in validated.columns:
            actuals = validated[target_col].reindex(result.index)
            if actuals.notna().any():
                result["actual_price"] = actuals

        return result

    def design_matrix(self, future_exog: pd.DataFrame) -> pd.DataFrame:
        """
        Returns the simulated design matrix for a horizon, without predicting.

        Needed by the SHAP layer, which explains the features the model saw.
        """
        frame = engineer_calendar_features(
            cast(pd.DataFrame, FutureExogenousSchema.validate(future_exog)),
            self.config,
        )
        return self.cascade.simulate(frame)[self.feature_names]

    @property
    def training_design_matrix(self) -> pd.DataFrame:
        """The training design matrix, used as SHAP background data."""
        return self.cascade.enriched_train[self.feature_names]


class PriceForecasterModel(mlflow.pyfunc.PythonModel):
    """
    MLflow pyfunc wrapper around a fitted PriceForecaster.

    Accepts a DataFrame with a `date` column and a `nuclear_avail` column — a
    JSON-friendly shape the API can build straight from a request body — and
    returns the forecast frame with the date as a column.
    """

    DATE_INPUT_COLUMN = "date"
    NUCLEAR_INPUT_COLUMN = "nuclear_avail"

    def __init__(self, forecaster: PriceForecaster | None = None) -> None:
        self.forecaster = forecaster

    def load_context(self, context: mlflow.pyfunc.PythonModelContext) -> None:
        # The forecaster travels with the pickled model object, so there is
        # nothing extra to load. Defined for interface completeness.
        return None

    def _to_exog_frame(self, model_input: pd.DataFrame) -> pd.DataFrame:
        assert self.forecaster is not None
        nuclear_col = self.forecaster.config["feature_engineering"]["nuclear_col"]

        frame = model_input.copy()

        if self.DATE_INPUT_COLUMN in frame.columns:
            frame = frame.rename(columns={self.DATE_INPUT_COLUMN: "Date"})
        if self.NUCLEAR_INPUT_COLUMN in frame.columns:
            frame = frame.rename(columns={self.NUCLEAR_INPUT_COLUMN: nuclear_col})

        return frame

    def predict(
        self,
        context: mlflow.pyfunc.PythonModelContext,
        model_input: pd.DataFrame,
        params: dict[str, Any] | None = None,
    ) -> pd.DataFrame:
        if self.forecaster is None:
            raise RuntimeError(
                "PriceForecasterModel has no forecaster attached; it was not "
                "constructed with a fitted PriceForecaster."
            )

        exog = self._to_exog_frame(model_input)
        result = self.forecaster.forecast(exog)

        return result.reset_index()

    def model_info(self) -> dict[str, Any]:
        """Provenance for the API's /model-info endpoint."""
        if self.forecaster is None:
            raise RuntimeError("PriceForecasterModel has no forecaster attached.")
        return self.forecaster.metadata.to_dict()
