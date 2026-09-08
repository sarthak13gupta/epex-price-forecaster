import numpy as np
import pandas as pd
import numpy.typing as npt
from sklearn.linear_model import LinearRegression, Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.exceptions import NotFittedError
from pandera.typing import DataFrame
from typing import Any, cast

from src.data.schema import ProcessedDataSchema
from src.features.build_features import (
    engineer_national_temperature,
    engineer_degree_days,
    engineer_residual_demand,
)

DAYS_IN_YEAR = 365


class RobustArcSinTransformer(BaseEstimator, TransformerMixin):
    """
    Applies MAD (Median Absolute Deviation) scaling and Inverse Hyperbolic Sine transformation.
    Designed specifically to handle extreme price spikes and negative prices in electricity markets.
    """
    # 1. Statically declare class attributes for Pylance
    median_: float | None
    mad_: float | None
    z_75: float

    def __init__(self) -> None:
        self.median_ = None
        self.mad_ = None
        self.z_75 = 0.6745

    def _check_is_fitted(self) -> None:
        """Type guard to ensure the model has been fitted."""
        if self.median_ is None or self.mad_ is None:
            raise NotFittedError(
                "This RobustArcSinTransformer instance is not fitted yet. "
                "Call 'fit' with appropriate arguments before using this estimator."
            )

    def fit(self, X: npt.ArrayLike, y: Any = None) -> "RobustArcSinTransformer":
        # Force float64 array to prevent mixed-type downstream math
        X_arr = np.asarray(X, dtype=np.float64)

        # Cast to strict float to satisfy Pylance (np.median returns np.float64 or Any)
        self.median_ = float(np.median(X_arr))
        self.mad_ = float(np.median(np.abs(X_arr - self.median_)))

        if self.mad_ == 0.0:
            self.mad_ = 1e-6

        return self

    def transform(self, X: npt.ArrayLike, y: Any = None) -> npt.NDArray[np.float64]:
        self._check_is_fitted()

        # 2. Local type narrowing: explicitly tells Pylance these are NOT None
        assert self.median_ is not None
        assert self.mad_ is not None

        X_arr = np.asarray(X, dtype=np.float64)

        # Robust Scaling
        y_t = (1.0 / self.z_75) * ((X_arr - self.median_) / self.mad_)
        return np.arcsinh(y_t)

    def inverse_transform(self, X: npt.ArrayLike, y: Any = None) -> npt.NDArray[np.float64]:
        self._check_is_fitted()

        # 2. Local type narrowing
        assert self.median_ is not None
        assert self.mad_ is not None

        X_arr = np.asarray(X, dtype=np.float64)

        # Inverse Robust Scaling
        return self.z_75 * np.sinh(X_arr) * self.mad_ + self.median_


class WeatherForecaster:
    """Trains a deterministic Fourier + AR lag model to forecast regional temperatures."""

    # 1. Statically declare class attributes for Pylance
    regions: list[str]
    coefficients: dict[str, dict[str, float]]
    start_date_hist: pd.Timestamp | None

    def __init__(self, region_columns: list[str]) -> None:
        self.regions = region_columns
        self.coefficients = {}
        self.start_date_hist = None

    def _remove_leap_days(self, df: pd.DataFrame) -> pd.DataFrame:
        # 2. Cast to DatetimeIndex to resolve "month is unknown" Pylance warning
        dt_index = pd.DatetimeIndex(df.index)
        return df[~((dt_index.month == 2) & (dt_index.day == 29))].copy()

    def fit(self, train_df: pd.DataFrame) -> "WeatherForecaster":
        # Remove Feb 29 to keep strict 365-day cycles
        df = self._remove_leap_days(train_df)

        # Explicitly cast to pd.Timestamp to ensure strict typing
        self.start_date_hist = pd.Timestamp(df.index[0])

        # Create time index (t)
        df['t'] = np.arange(1, len(df) + 1)

        # Fourier Features
        df['cos_1'] = np.cos(2 * np.pi * 1 * df['t'] / DAYS_IN_YEAR)
        df['sin_1'] = np.sin(2 * np.pi * 1 * df['t'] / DAYS_IN_YEAR)
        df['cos_2'] = np.cos(2 * np.pi * 2 * df['t'] / DAYS_IN_YEAR)
        df['sin_2'] = np.sin(2 * np.pi * 2 * df['t'] / DAYS_IN_YEAR)

        base_features = ['t', 'cos_1', 'sin_1', 'cos_2', 'sin_2']

        for region in self.regions:
            lag_1_col = f'{region}_lag_1'
            lag_2_col = f'{region}_lag_2'

            df[lag_1_col] = df[region].shift(1)
            df[lag_2_col] = df[region].shift(2)

            region_features = base_features + [lag_1_col, lag_2_col]
            train_subset = df.dropna(subset=region_features + [region])

            X = train_subset[region_features]
            y = train_subset[region]

            model = LinearRegression()
            model.fit(X, y)

            # Cast coefficients to float to ensure strict dictionary typing
            self.coefficients[region] = {
                'a': float(model.intercept_),
                'b': float(model.coef_[0]),
                'alpha_1': float(model.coef_[1]),
                'beta_1': float(model.coef_[2]),
                'alpha_2': float(model.coef_[3]),
                'beta_2': float(model.coef_[4]),
                'phi_1': float(model.coef_[5]),
                'phi_2': float(model.coef_[6]),
            }

        return self

    def predict(
        self,
        forecast_start_date: pd.Timestamp,
        horizon: int,
        last_two_days_actuals: pd.DataFrame,
    ) -> pd.DataFrame:
        # 3. Type Guard: Proves to Pylance that self.start_date_hist is NOT None
        if self.start_date_hist is None:
            raise NotFittedError("WeatherForecaster is not fitted yet. Call 'fit' first.")

        if len(last_two_days_actuals) < 2:
            raise ValueError(
                "WeatherForecaster.predict needs the last two days of observed "
                f"temperatures to seed its AR(2) lags, got {len(last_two_days_actuals)}."
            )

        dates = pd.date_range(start=forecast_start_date, periods=horizon, freq='D')

        # Explicitly type the dictionary so Pylance knows it holds a list of floats
        predicted_paths: dict[str, list[float]] = {region: [] for region in self.regions}

        # Now Pylance allows the subtraction because start_date_hist is guaranteed to be a Timestamp
        t_start = (forecast_start_date - self.start_date_hist).days

        for i, current_date in enumerate(dates):
            is_leap = (current_date.month == 2) and (current_date.day == 29)
            t_current = t_start + i if not is_leap else t_start + i - 1

            for region in self.regions:
                coefs = self.coefficients[region]

                deterministic = (
                    coefs['a'] +
                    coefs['b'] * t_current +
                    coefs['alpha_1'] * np.cos(2 * np.pi * 1 * t_current / DAYS_IN_YEAR) +
                    coefs['beta_1'] * np.sin(2 * np.pi * 1 * t_current / DAYS_IN_YEAR) +
                    coefs['alpha_2'] * np.cos(2 * np.pi * 2 * t_current / DAYS_IN_YEAR) +
                    coefs['beta_2'] * np.sin(2 * np.pi * 2 * t_current / DAYS_IN_YEAR)
                )

                # Cast extracted values to float for type safety
                if i == 0:
                    lag_1 = float(last_two_days_actuals.iloc[-1][region])
                    lag_2 = float(last_two_days_actuals.iloc[-2][region])
                elif i == 1:
                    lag_1 = predicted_paths[region][-1]
                    lag_2 = float(last_two_days_actuals.iloc[-1][region])
                else:
                    lag_1 = predicted_paths[region][-1]
                    lag_2 = predicted_paths[region][-2]

                pred_t = deterministic + (coefs['phi_1'] * lag_1) + (coefs['phi_2'] * lag_2)
                predicted_paths[region].append(pred_t)

        return pd.DataFrame(predicted_paths, index=dates)


class LoadForecaster:
    """Forecasts power demand using Ridge Regression on calendar and thermal features."""

    # Declare class attributes for strict typing
    features: list[str]
    model: Pipeline

    def __init__(
        self,
        calendar_features: list[str],
        hdd_cdd_columns: list[str],
        alpha: float = 1.0,
        random_state: int = 42,
    ) -> None:
        self.features = calendar_features + hdd_cdd_columns
        self.model = Pipeline([
            ('scaler', StandardScaler()),
            ('ridge', Ridge(alpha=alpha, random_state=random_state))
        ])

    def fit(self, train_df: pd.DataFrame, target_col: str) -> "LoadForecaster":
        df = train_df.dropna(subset=self.features + [target_col])
        X = df[self.features]
        y = df[target_col]
        self.model.fit(X, y)
        return self

    def predict(self, forecasted_features_df: pd.DataFrame) -> pd.Series:
        X = forecasted_features_df[self.features]
        predictions = self.model.predict(X)
        return pd.Series(
            predictions, index=forecasted_features_df.index, name='forecasted_demand'
        )


class ExogenousCascade:
    """
    The four-stage simulation that manufactures the price model's inputs.

        NUCLEAR AVAIL. (known ahead)
          1. WeatherForecaster  -> regional temperatures
          2. national temp + EWM inertia (T_lisse, Delta_T) and HDD/CDD
          3. LoadForecaster     -> DEMAND (MW)
          4. Residual_Demand = DEMAND - NUCLEAR (+ polynomial terms)

    Fitting and simulation are split so the same fitted cascade can be persisted
    alongside the price model and replayed at serving time, instead of being
    refit on every request the way the notebook does.
    """

    def __init__(
        self,
        config: dict[str, Any],
        calendar_features: list[str],
        hdd_cdd_columns: list[str],
    ) -> None:
        self.config = config
        self.calendar_features = calendar_features
        self.hdd_cdd_columns = hdd_cdd_columns

        fe = config["feature_engineering"]
        self.region_cols: list[str] = list(config["features"]["weather"])
        self.hdd_cdd_reference: list[str] = list(fe["hdd_cdd_reference"])
        self.demand_col: str = fe["demand_col"]
        self.nuclear_col: str = fe["nuclear_col"]
        self.warmup_days: int = int(config["backtest"]["warmup_days"])

        self.weather_model_: WeatherForecaster | None = None
        self.load_model_: LoadForecaster | None = None
        self.t_norm_reference_: pd.Series | None = None
        self.enriched_train_: pd.DataFrame | None = None
        self.warmup_frame_: pd.DataFrame | None = None
        self.train_end_: pd.Timestamp | None = None

    def _check_is_fitted(self) -> None:
        if self.weather_model_ is None or self.load_model_ is None:
            raise NotFittedError(
                "ExogenousCascade is not fitted yet. Call 'fit' first."
            )

    def _add_thermal_features(
        self, df: pd.DataFrame, is_train: bool
    ) -> tuple[pd.DataFrame, pd.Series]:
        """
        Builds the thermal block. Degree days are derived from the smoothed
        national temperature, so national temperature must be computed first.
        """
        enriched, t_norm_reference = engineer_national_temperature(
            cast(DataFrame[ProcessedDataSchema], df),
            self.config,
            is_train=is_train,
            t_norm_reference=None if is_train else self.t_norm_reference_,
        )
        enriched = engineer_degree_days(
            enriched, self.hdd_cdd_reference, self.config
        )
        return enriched, t_norm_reference

    def fit(self, train_data: DataFrame[ProcessedDataSchema]) -> "ExogenousCascade":
        """Fits the weather and load models and builds the enriched training frame."""
        train_frame = train_data.copy()

        # Stage 1: weather. Fit on the raw regional series.
        self.weather_model_ = WeatherForecaster(
            region_columns=self.region_cols
        ).fit(train_frame)

        # Stage 2: thermal features, which also establishes the seasonal norm.
        enriched_train, self.t_norm_reference_ = self._add_thermal_features(
            train_frame, is_train=True
        )

        # Stage 4 on the training side, so the price model trains on the same
        # residual-demand basis it will see at inference time.
        enriched_train = engineer_residual_demand(
            enriched_train, self.demand_col, self.nuclear_col
        )

        # Stage 3: load. Fit on observed demand.
        self.load_model_ = LoadForecaster(
            calendar_features=self.calendar_features,
            hdd_cdd_columns=self.hdd_cdd_columns,
            random_state=int(self.config["models"]["random_state"]),
        ).fit(enriched_train, self.demand_col)

        # Retain the tail needed to seed a forecast: AR(2) lags and enough
        # history for the EWM thermal inertia to start warm.
        self.warmup_frame_ = enriched_train.iloc[-self.warmup_days:].copy()
        self.train_end_ = pd.Timestamp(enriched_train.index[-1])
        self.enriched_train_ = enriched_train.drop(
            columns=self.region_cols, errors='ignore'
        )

        return self

    def simulate(self, future_frame: pd.DataFrame) -> pd.DataFrame:
        """
        Runs the cascade over a forward-looking frame.

        `future_frame` must carry a gap-free daily index, the calendar features
        and the known nuclear availability. Its temperature and demand columns
        are overwritten with simulated values.
        """
        self._check_is_fitted()
        assert self.weather_model_ is not None
        assert self.load_model_ is not None
        assert self.warmup_frame_ is not None

        if future_frame.empty:
            raise ValueError("Cannot simulate an empty forecast horizon.")

        simulated = future_frame.copy()
        forecast_start = pd.Timestamp(simulated.index[0])
        forecast_end = pd.Timestamp(simulated.index[-1])

        if self.nuclear_col not in simulated.columns:
            raise KeyError(
                f"Forecast horizon is missing the known-ahead input "
                f"{self.nuclear_col!r}."
            )
        if simulated[self.nuclear_col].isna().any():
            raise ValueError(
                f"{self.nuclear_col} contains missing values over the forecast "
                "horizon; it is a required input, not a simulated one."
            )

        missing_calendar = [
            col for col in self.calendar_features if col not in simulated.columns
        ]
        if missing_calendar:
            raise KeyError(
                f"Forecast horizon is missing calendar feature(s) {missing_calendar}. "
                "Run engineer_calendar_features on the frame before simulating."
            )

        if self.train_end_ is not None and forecast_start <= self.train_end_:
            raise ValueError(
                f"Forecast horizon starts at {forecast_start.date()}, which overlaps "
                f"the training window ending {self.train_end_.date()}. The warm-up "
                "stitch requires the horizon to begin after the training data."
            )

        # Stage 1: forecast regional temperatures.
        forecasted_weather = self.weather_model_.predict(
            forecast_start_date=forecast_start,
            horizon=len(simulated),
            last_two_days_actuals=self.warmup_frame_,
        )
        for col in self.region_cols:
            simulated[col] = forecasted_weather[col].values

        # Stage 2: stitch real history in front of the forecast so the EWM
        # inertia is continuous, smooth, then slice the horizon back out.
        stitched = pd.concat([self.warmup_frame_, simulated])
        stitched, _ = self._add_thermal_features(stitched, is_train=False)
        simulated = stitched.loc[forecast_start:forecast_end].copy()

        # Stage 3: forecast demand from the simulated thermal state.
        simulated[self.demand_col] = self.load_model_.predict(simulated).values

        # Stage 4: rebuild residual demand from the simulated demand.
        simulated = engineer_residual_demand(
            simulated, self.demand_col, self.nuclear_col
        )

        return simulated.drop(columns=self.region_cols, errors='ignore')

    @property
    def enriched_train(self) -> pd.DataFrame:
        """The training frame with the full feature block, ready for the price model."""
        if self.enriched_train_ is None:
            raise NotFittedError("ExogenousCascade is not fitted yet. Call 'fit' first.")
        return self.enriched_train_


def simulate_exogenous_pipeline(
    train_data: DataFrame[ProcessedDataSchema],
    test_data: DataFrame[ProcessedDataSchema],
    test_start: pd.Timestamp,
    test_end: pd.Timestamp,
    calendar_features: list[str],
    hdd_cdd_cols: list[str],
    config: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Fits the cascade on `train_data` and simulates the exogenous inputs over
    `test_data`, returning (enriched_train, simulated_test).

    Thin wrapper kept for the backtest loop, which refits per fold by design.
    """
    cascade = ExogenousCascade(
        config=config,
        calendar_features=calendar_features,
        hdd_cdd_columns=hdd_cdd_cols,
    ).fit(train_data)

    simulated_test = cascade.simulate(test_data.loc[test_start:test_end])

    return cascade.enriched_train, simulated_test
