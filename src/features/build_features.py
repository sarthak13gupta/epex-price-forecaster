from typing import Any, cast

import holidays
import numpy as np
import pandas as pd
from pandera.typing import DataFrame

from src.data.schema import ProcessedDataSchema

DAYS_IN_YEAR = 365
LEAP_DAY_OF_YEAR = 366


def engineer_calendar_features(
    df: pd.DataFrame, config: dict[str, Any]
) -> pd.DataFrame:
    """Extracts calendar-based indicators and French industrial-regime flags."""
    df_eng = df.copy()

    # Explicitly cast to DatetimeIndex to resolve Pylance type warnings
    dt_index = pd.DatetimeIndex(df_eng.index)

    # 1. Base Calendar Features
    df_eng['year'] = dt_index.year
    df_eng['month'] = dt_index.month
    df_eng['day_of_year'] = dt_index.dayofyear
    df_eng['day_of_week'] = dt_index.dayofweek
    df_eng['is_weekend'] = df_eng['day_of_week'].isin([5, 6]).astype(int)

    # 2. Holidays. Compare on date objects rather than datetime64 values, which
    # pandas deprecated for .isin against a holidays mapping.
    unique_years = dt_index.year.unique().tolist()
    fr_holidays = holidays.France(years=unique_years)
    holiday_dates = set(fr_holidays.keys())
    df_eng['is_holiday'] = pd.Index(dt_index.date).isin(holiday_dates).astype(int)

    # 3. COVID-19 Lockdown
    lockdown_start = pd.Timestamp(config["feature_engineering"]["lockdown_start"])
    lockdown_end = pd.Timestamp(config["feature_engineering"]["lockdown_end"])
    df_eng['is_lockdown'] = (
        (dt_index >= lockdown_start) & (dt_index <= lockdown_end)
    ).astype(int)

    # 4. French Summer Vacation Phases
    month = dt_index.month
    day = dt_index.day
    day_of_week = dt_index.dayofweek

    # Phase A: School End July (July 1 to July 13)
    df_eng['is_school_end_july'] = ((month == 7) & (day >= 1) & (day <= 13)).astype(int)

    # Phase B: Bastille Day & Dynamic Bridge Days
    is_bastille = (month == 7) & (day == 14)
    is_monday_bridge = (month == 7) & (day == 13) & (day_of_week == 0)
    is_friday_bridge = (month == 7) & (day == 15) & (day_of_week == 4)
    df_eng['is_bastille_bridge'] = (
        is_bastille | is_monday_bridge | is_friday_bridge
    ).astype(int)

    # Phase C: Grandes Vacances (July 15 to July 31)
    df_eng['is_grandes_vacances_july'] = (
        (month == 7) & (day >= 15) & (day <= 31)
    ).astype(int)

    # Enforce non-overlapping boundaries for bridge days
    df_eng.loc[df_eng['is_bastille_bridge'] == 1, 'is_school_end_july'] = 0
    df_eng.loc[df_eng['is_bastille_bridge'] == 1, 'is_grandes_vacances_july'] = 0

    # August Trough
    df_eng['is_august_vacation'] = (month == 8).astype(int)

    # 5. Cyclical Encoding
    df_eng['sin_day_of_year'] = np.sin(
        2 * np.pi * df_eng['day_of_year'] / DAYS_IN_YEAR
    )
    df_eng['cos_day_of_year'] = np.cos(
        2 * np.pi * df_eng['day_of_year'] / DAYS_IN_YEAR
    )

    return df_eng


def engineer_residual_demand(
    df: pd.DataFrame, demand_col: str, nuclear_col: str
) -> pd.DataFrame:
    """Calculates residual demand and its polynomial transformations."""
    df_eng = df.copy()

    df_eng['Residual_Demand'] = df_eng[demand_col] - df_eng[nuclear_col]
    df_eng['Residual_Demand_sq'] = df_eng['Residual_Demand'] ** 2
    df_eng['Residual_Demand_cu'] = df_eng['Residual_Demand'] ** 3

    return df_eng


def engineer_degree_days(
    df: pd.DataFrame, temp_features: list[str], config: dict[str, Any]
) -> pd.DataFrame:
    """
    Converts temperature columns into Heating and Cooling Degree Days.

    `temp_features` names the source columns; pass
    config["feature_engineering"]["hdd_cdd_reference"] to derive degree days
    from the smoothed national temperature (T_lisse), which is what the price
    model consumes.
    """
    df_eng = df.copy()
    heating_threshold = config["feature_engineering"]["heating_threshold_c"]
    cooling_threshold = config["feature_engineering"]["cooling_threshold_c"]

    missing = [col for col in temp_features if col not in df_eng.columns]
    if missing:
        raise KeyError(
            f"Cannot build degree days, missing source column(s): {missing}. "
            "engineer_national_temperature must run first when the reference "
            "is a derived column such as T_lisse."
        )

    for col in temp_features:
        df_eng[f'{col}_HDD'] = np.maximum(heating_threshold - df_eng[col], 0)
        df_eng[f'{col}_CDD'] = np.maximum(df_eng[col] - cooling_threshold, 0)

    return df_eng


def _build_seasonal_reference(smoothed: pd.Series, dt_index: pd.DatetimeIndex) -> pd.Series:
    """
    Builds the day-of-year climatology of the smoothed national temperature.

    A rolling 731-day training window need not contain a Feb 29, which would
    leave day-of-year 366 unmapped and produce a NaN Delta_T on leap days. Day
    366 therefore falls back to day 365.
    """
    reference = smoothed.groupby(dt_index.dayofyear).mean()

    if LEAP_DAY_OF_YEAR not in reference.index and DAYS_IN_YEAR in reference.index:
        reference.loc[LEAP_DAY_OF_YEAR] = reference.loc[DAYS_IN_YEAR]

    return reference.sort_index()


def engineer_national_temperature(
    df: DataFrame[ProcessedDataSchema],
    config: dict[str, Any],
    is_train: bool = True,
    t_norm_reference: pd.Series | None = None,
) -> tuple[pd.DataFrame, pd.Series]:
    """Calculates weighted national temperature, thermal inertia, and seasonal deviation."""
    df_eng = df.copy()
    weights = config["feature_engineering"]["temp_weights"]
    alpha = config["feature_engineering"]["ewm_alpha"]

    # Explicitly cast to DatetimeIndex to resolve Pylance type warnings
    dt_index = pd.DatetimeIndex(df_eng.index)

    # Calculate T_raw (weighted average)
    df_eng['T_raw'] = sum(
        df_eng[region] * weight for region, weight in weights.items()
    )

    # Calculate T_lisse (Exponential Smoothing for building inertia)
    df_eng['T_lisse'] = df_eng['T_raw'].ewm(alpha=alpha).mean()

    # Calculate or apply seasonal reference mapping
    if is_train:
        t_norm_reference = _build_seasonal_reference(df_eng['T_lisse'], dt_index)

    if t_norm_reference is None:
        raise ValueError("t_norm_reference must be provided when is_train=False")

    df_eng['T_norm'] = dt_index.dayofyear.map(t_norm_reference.to_dict())

    if df_eng['T_norm'].isna().any():
        unmapped = sorted(
            set(dt_index[df_eng['T_norm'].isna()].dayofyear.tolist())
        )
        raise ValueError(
            f"Seasonal temperature reference has no entry for day(s) of year "
            f"{unmapped}. The training window is too short to cover the "
            "forecast calendar."
        )

    df_eng['Delta_T'] = df_eng['T_lisse'] - df_eng['T_norm']

    return df_eng, t_norm_reference


def build_static_features(
    df: DataFrame[ProcessedDataSchema], config: dict[str, Any]
) -> pd.DataFrame:
    """
    Applies the feature engineering that needs no forecasting.

    Calendar flags depend only on the index and residual demand only on columns
    already present, so both can be computed once over the whole history. The
    thermal features (T_lisse, Delta_T, HDD/CDD) and simulated demand are
    fold-specific and are built inside the exogenous simulation cascade instead,
    because they depend on a training window.
    """
    fe = config["feature_engineering"]

    df_eng = engineer_calendar_features(df, config)
    df_eng = engineer_residual_demand(df_eng, fe["demand_col"], fe["nuclear_col"])

    return df_eng


def drop_incomplete_rows(
    df: pd.DataFrame, config: dict[str, Any]
) -> DataFrame[ProcessedDataSchema]:
    """
    Drops rows missing any of the base inputs the cascade and price model need.

    Deliberately checks only the columns that exist at this stage: the thermal
    and simulated-demand features are produced per fold and must not gate this
    global cleaning step.
    """
    features = config["features"]
    target = config["dataset"]["target"]

    required = (
        list(features["calendar"])
        + list(features["weather"])
        + list(features["residual_demand"])
        + list(features["market"])
        + [target]
    )

    before = len(df)
    cleaned = df.dropna(subset=required)
    dropped = before - len(cleaned)

    if dropped:
        print(f"Dropped {dropped} rows with incomplete base inputs.")

    return cast(DataFrame[ProcessedDataSchema], cleaned)
