"""
Leaderboard construction and residual diagnostics.

Plots are written to disk rather than shown, so the same code runs headless in
a container and the figures can be logged to MLflow as run artifacts.
"""

import os
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

METRIC_COLUMNS = ("mae", "rmse")


def build_leaderboard(
    metrics_df: pd.DataFrame, selection_metric: str = "mae"
) -> pd.DataFrame:
    """
    Aggregates per-fold metrics into one row per model, best first.

    Folds cover unequal month lengths, so the mean is taken over folds (each
    fold weighted equally) to match how the backtest reports them.
    """
    if selection_metric not in METRIC_COLUMNS:
        raise ValueError(
            f"Unknown selection metric {selection_metric!r}. "
            f"Expected one of {list(METRIC_COLUMNS)}."
        )

    leaderboard = (
        metrics_df
        .groupby("model", as_index=False)
        .agg(
            mae=("mae", "mean"),
            rmse=("rmse", "mean"),
            mae_std=("mae", "std"),
            rmse_std=("rmse", "std"),
            worst_fold_mae=("mae", "max"),
            n_folds=("fold", "count"),
        )
        .sort_values(by=selection_metric)
        .reset_index(drop=True)
    )

    return leaderboard


def select_best_model(
    leaderboard: pd.DataFrame, selection_metric: str = "mae"
) -> str:
    """Returns the name of the leading model on the leaderboard."""
    if leaderboard.empty:
        raise ValueError("Cannot select a best model from an empty leaderboard.")
    return str(leaderboard.sort_values(by=selection_metric).iloc[0]["model"])


def summarize_by_regime(metrics_df: pd.DataFrame) -> pd.DataFrame:
    """
    Pivots per-fold MAE into model x regime, exposing where a model is weak.

    A model can win on average while being unusable in a specific regime (the
    2020 lockdown folds are the obvious case here), which a single mean hides.
    """
    return (
        metrics_df
        .pivot_table(index="regime", columns="model", values="mae", aggfunc="mean")
        .round(2)
    )


def plot_residual_diagnostics(
    results_df: pd.DataFrame, model_name: str, output_dir: str
) -> str:
    """
    Writes a 4-panel residual diagnostic for one model and returns the path.

    Panels: predicted vs actual, residuals over time, residual distribution,
    and residuals by day of week.
    """
    df = results_df[results_df["model"] == model_name].copy()

    if df.empty:
        raise ValueError(f"No backtest predictions found for model {model_name!r}.")

    df["residual"] = df["actual_price"] - df["predicted_price"]
    df["day_of_week"] = pd.DatetimeIndex(df.index).dayofweek

    sns.set_theme(style="whitegrid")
    fig, axes = plt.subplots(2, 2, figsize=(16, 12))

    # Predicted vs. actual, against the y=x ideal.
    sns.scatterplot(
        data=df, x="actual_price", y="predicted_price",
        alpha=0.5, ax=axes[0, 0], color="teal",
    )
    lo = float(min(df["actual_price"].min(), df["predicted_price"].min()))
    hi = float(max(df["actual_price"].max(), df["predicted_price"].max()))
    axes[0, 0].plot([lo, hi], [lo, hi], color="red", linestyle="--", linewidth=2)
    axes[0, 0].set_title(
        f"[{model_name}] Predicted vs. Actual", fontsize=14, fontweight="bold"
    )
    axes[0, 0].set_xlabel("Actual Price (EUR/MWh)")
    axes[0, 0].set_ylabel("Predicted Price (EUR/MWh)")

    # Residuals over time: exposes regime drift.
    sns.scatterplot(
        data=df, x=df.index, y="residual", alpha=0.5, ax=axes[0, 1], color="blue"
    )
    axes[0, 1].axhline(0, color="red", linestyle="--", linewidth=2)
    axes[0, 1].set_title("Residuals Over Time", fontsize=14, fontweight="bold")
    axes[0, 1].set_ylabel("Error (Actual - Predicted)")

    # Residual distribution: exposes bias and fat tails.
    sns.histplot(df["residual"], kde=True, ax=axes[1, 0], color="purple")
    axes[1, 0].axvline(0, color="red", linestyle="--", linewidth=2)
    axes[1, 0].set_title(
        "Distribution of Residuals", fontsize=14, fontweight="bold"
    )
    axes[1, 0].set_xlabel("Error (EUR/MWh)")

    # Residuals by weekday: exposes an unmodelled weekly cycle.
    sns.boxplot(
        data=df, x="day_of_week", y="residual", hue="day_of_week",
        ax=axes[1, 1], palette="magma", legend=False,
    )
    axes[1, 1].axhline(0, color="red", linestyle="--", linewidth=2)
    axes[1, 1].set_title("Errors by Day of Week", fontsize=14, fontweight="bold")
    axes[1, 1].set_xlabel("Day of Week (0 = Monday, 6 = Sunday)")

    fig.tight_layout()

    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"residuals_{model_name}.png")
    fig.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)

    return output_path


def plot_backtest_predictions(
    results_df: pd.DataFrame, model_name: str, output_dir: str
) -> str:
    """Writes actual vs predicted price paths across all backtest folds."""
    df = results_df[results_df["model"] == model_name].sort_index()

    if df.empty:
        raise ValueError(f"No backtest predictions found for model {model_name!r}.")

    sns.set_theme(style="whitegrid")
    fig, ax = plt.subplots(figsize=(16, 6))

    # Folds are non-contiguous months, so plot each separately to avoid drawing
    # a misleading line across the gaps between them.
    for fold, fold_df in df.groupby("fold"):
        label_actual = "Actual" if fold == df["fold"].min() else None
        label_pred = "Predicted" if fold == df["fold"].min() else None
        ax.plot(
            fold_df.index, fold_df["actual_price"],
            color="black", linewidth=1.5, label=label_actual,
        )
        ax.plot(
            fold_df.index, fold_df["predicted_price"],
            color="crimson", linewidth=1.5, linestyle="--", label=label_pred,
        )

    ax.set_title(
        f"[{model_name}] Walk-Forward Backtest: Actual vs Predicted",
        fontsize=14, fontweight="bold",
    )
    ax.set_ylabel("Price (EUR/MWh)")
    ax.legend()
    fig.tight_layout()

    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"backtest_predictions_{model_name}.png")
    fig.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)

    return output_path


def plot_forecast(
    forecast_df: pd.DataFrame, output_dir: str, filename: str = "holdout_forecast.png"
) -> str:
    """
    Writes the forward forecast, overlaying actuals where they exist.

    In live use the actuals are absent, so the overlay is conditional.
    """
    sns.set_theme(style="whitegrid")
    fig, ax = plt.subplots(figsize=(14, 6))

    ax.plot(
        forecast_df.index, forecast_df["predicted_price"],
        color="crimson", linewidth=2, marker="o", markersize=4, label="Forecast",
    )

    if "actual_price" in forecast_df.columns and forecast_df["actual_price"].notna().any():
        ax.plot(
            forecast_df.index, forecast_df["actual_price"],
            color="black", linewidth=2, label="Actual",
        )

    ax.set_title(
        "Day-Ahead Spot Price Forecast", fontsize=14, fontweight="bold"
    )
    ax.set_ylabel("Price (EUR/MWh)")
    ax.legend()
    fig.tight_layout()

    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, filename)
    fig.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)

    return output_path


def metrics_to_mlflow_dict(
    leaderboard_row: pd.Series, prefix: str = ""
) -> dict[str, float]:
    """Flattens one leaderboard row into scalar metrics MLflow can log."""
    keys = ("mae", "rmse", "mae_std", "rmse_std", "worst_fold_mae")
    return {
        f"{prefix}{key}": float(leaderboard_row[key])
        for key in keys
        if key in leaderboard_row and pd.notna(leaderboard_row[key])
    }


def per_regime_metrics_to_mlflow_dict(
    metrics_df: pd.DataFrame, model_name: str
) -> dict[str, float]:
    """
    Turns per-fold MAE into MLflow-safe metric names, one per fold.

    Regime labels contain spaces, slashes and parentheses that MLflow rejects,
    so folds are keyed by index and the labels live in the metrics table
    artifact instead.
    """
    subset = metrics_df[metrics_df["model"] == model_name]
    return {
        f"fold_{int(row['fold']):02d}_mae": float(row["mae"])
        for _, row in subset.iterrows()
    }


def print_leaderboard(leaderboard: pd.DataFrame, selection_metric: str) -> None:
    """Prints the leaderboard in the notebook's format."""
    print("\n" + "=" * 60)
    print(f"FINAL MODEL LEADERBOARD (sorted by mean fold {selection_metric.upper()})")
    print("=" * 60)
    display_cols = ["model", "mae", "rmse", "mae_std", "worst_fold_mae", "n_folds"]
    print(leaderboard[display_cols].round(3).to_string(index=False))
