import os
from typing import Any, cast

import pandas as pd
from pandera.typing import DataFrame

from src.utils.config_loader import load_config
from src.data.data_loader import load_raw_dataset
from src.data.schema import (
    RawDataSchema,
    ProcessedDataSchema,
    FutureExogenousSchema,
)


def _set_datetime_index(df: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    """Parses the date column into a sorted DatetimeIndex named 'Date'."""
    date_col = config["dataset"]["date_col"]
    date_format = config["dataset"]["date_format"]

    if date_col in df.columns:
        df[date_col] = pd.to_datetime(df[date_col], format=date_format)
        df = df.set_index(date_col)
    else:
        df.index = pd.to_datetime(df.index)

    df = df.sort_index()
    df.index.name = "Date"
    return df


def _drop_duplicate_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Keeps the first observation for any repeated date."""
    if df.index.duplicated().any():
        dupes = int(df.index.duplicated().sum())
        print(f"Removing {dupes} duplicate index entries.")
        df = df[~df.index.duplicated(keep="first")]
    return df


def preprocess_data(
    df: pd.DataFrame, config: dict[str, Any]
) -> DataFrame[ProcessedDataSchema]:
    """
    Cleans training data: drops duplicate dates, enforces a gap-free daily
    index, and time-interpolates the resulting holes.

    Target transformation (arcsinh) and feature engineering are deliberately
    left to build_features.py / the model pipelines to keep the concerns split.
    """
    print("Preprocessing data...")

    freq = config["dataset"]["freq"]

    df = _set_datetime_index(df, config)

    df = RawDataSchema.validate(df)
    print("Raw data schema validated.")

    df = _drop_duplicate_dates(df)

    expected_days = pd.date_range(df.index.min(), df.index.max(), freq=freq)
    missing_days = int(expected_days[~expected_days.isin(df.index)].shape[0])

    if missing_days > 0:
        print(f"Reindexing and interpolating {missing_days} missing days...")
        df = df.reindex(expected_days)
        df.index.name = "Date"
        df = df.interpolate(method="time", limit_direction="both")
    else:
        print("No missing days found. Frequency is continuous.")

    processed = ProcessedDataSchema.validate(df)
    print("Processed data schema validated.")
    print("Preprocessing complete.")

    return cast(DataFrame[ProcessedDataSchema], processed)


def prepare_future_exogenous(
    df: pd.DataFrame, config: dict[str, Any]
) -> DataFrame[FutureExogenousSchema]:
    """
    Cleans forward-looking inference input.

    Unlike training data this frame is mostly empty by design: only nuclear
    availability is known ahead of time, so the unknowable columns are left as
    NaN for the exogenous simulation cascade to fill. Only the nuclear series is
    required to be gap-free, since it is the one real input to the cascade.
    """
    print("Preparing future exogenous inputs...")

    freq = config["dataset"]["freq"]
    nuclear_col = config["feature_engineering"]["nuclear_col"]

    df = _set_datetime_index(df, config)
    df = _drop_duplicate_dates(df)

    expected_days = pd.date_range(df.index.min(), df.index.max(), freq=freq)
    missing_days = int(expected_days[~expected_days.isin(df.index)].shape[0])

    if missing_days > 0:
        print(f"Reindexing {missing_days} missing days in the forecast horizon...")
        df = df.reindex(expected_days)
        df.index.name = "Date"

    # Interpolate only the known-ahead series. Interpolating the simulated
    # columns would silently invent exogenous values the cascade is meant to
    # produce, so they stay NaN.
    if nuclear_col in df.columns and df[nuclear_col].isna().any():
        gaps = int(df[nuclear_col].isna().sum())
        print(f"Interpolating {gaps} missing values in {nuclear_col}.")
        df[nuclear_col] = df[nuclear_col].interpolate(
            method="time", limit_direction="both"
        )

    future = FutureExogenousSchema.validate(df)
    print(f"Future exogenous inputs validated: {len(future)} days.")

    return cast(DataFrame[FutureExogenousSchema], future)


def run_preprocessing() -> None:
    """Ingests and cleans the training data, then persists it as Parquet."""
    config = load_config()

    df_raw = load_raw_dataset(dataset_type="train")
    df_processed = preprocess_data(df_raw, config)

    output_path = config["paths"]["processed_parquet"]
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    df_processed.to_parquet(output_path, engine="pyarrow")

    print(f"[SUCCESS] Processed dataset saved to: {output_path}")


if __name__ == "__main__":
    run_preprocessing()
