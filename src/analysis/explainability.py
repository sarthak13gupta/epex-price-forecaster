"""
SHAP attribution for the winning price model.

Two views, as in the notebook:

  - model-agnostic: explains the full pipeline including the inverse arcsinh
    transform, so contributions are in EUR/MWh and readable by a trader. Slow,
    because it permutes through the whole wrapper.
  - model-specific: explains the inner algorithm on the scaled, transformed
    target. Fast and exact, but the units are arcsinh-space.

Figures are written to disk so they can be logged to MLflow and rendered by the
Streamlit frontend.
"""

import contextlib
import os
import warnings
from typing import Any, Iterator

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import shap
import xgboost as xgb
from sklearn.linear_model import ElasticNet, Lasso, LinearRegression, Ridge


@contextlib.contextmanager
def _suppress_shap_rng_warning() -> Iterator[None]:
    """
    Silences one specific third-party deprecation.

    shap's PermutationExplainer calls np.random.seed unconditionally in its
    constructor, even when no seed is supplied, which NumPy now warns about.
    There is no argument that avoids it, so it is filtered narrowly by message
    rather than by blanket-ignoring FutureWarning across the pipeline.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="The NumPy global RNG was seeded",
            category=FutureWarning,
        )
        yield


def _unwrap_pipeline(model: Any) -> tuple[Any, Any]:
    """
    Pierces a TransformedTargetRegressor to reach the scaler and the algorithm.

    Returns (scaler, algorithm). Either may be None when the model is not the
    expected scaler+model pipeline shape (the naive baseline, for instance).
    """
    inner = getattr(model, "regressor_", None) or getattr(model, "regressor", None)

    if inner is None:
        inner = model

    named_steps = getattr(inner, "named_steps", None)
    if named_steps is None:
        return None, inner

    return named_steps.get("scaler"), named_steps.get("model")


def _save_summary_plot(
    shap_values: Any, X: pd.DataFrame, title: str, output_path: str
) -> str:
    """Renders and saves a SHAP beeswarm summary."""
    plt.figure(figsize=(10, 8))
    with _suppress_shap_rng_warning():
        shap.summary_plot(shap_values, X, show=False)
    plt.title(title, fontsize=13, fontweight="bold")
    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close("all")
    return output_path


def _save_waterfall_plot(
    shap_values: Any, row: int, title: str, output_path: str
) -> str:
    """Renders and saves a single-prediction SHAP waterfall."""
    plt.figure(figsize=(10, 8))
    with _suppress_shap_rng_warning():
        shap.plots.waterfall(shap_values[row], show=False)
    plt.title(title, fontsize=13, fontweight="bold")
    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close("all")
    return output_path


def explain_model_agnostic(
    model: Any,
    X_train: pd.DataFrame,
    X_test: pd.DataFrame,
    output_dir: str,
    background_samples: int = 100,
    random_state: int = 42,
) -> dict[str, Any]:
    """
    Explains the full pipeline in EUR/MWh via a permutation explainer.

    The background set is subsampled because the explainer's cost scales with
    it, and a few hundred rows already characterise the feature distribution.
    """
    print("Calculating model-agnostic SHAP values (EUR/MWh)...")

    # Subsample with pandas rather than shap.sample, which seeds NumPy's global
    # RNG internally and trips a FutureWarning.
    n_background = min(background_samples, len(X_train))
    background = X_train.sample(n=n_background, random_state=random_state)

    with _suppress_shap_rng_warning():
        explainer = shap.Explainer(model.predict, background)
        shap_values = explainer(X_test)

    os.makedirs(output_dir, exist_ok=True)
    summary_path = _save_summary_plot(
        shap_values, X_test,
        "SHAP Global Feature Importance - Model Agnostic (EUR/MWh)",
        os.path.join(output_dir, "shap_summary_agnostic.png"),
    )
    waterfall_path = _save_waterfall_plot(
        shap_values, 0,
        f"SHAP Contribution Breakdown - {X_test.index[0].date()} (EUR/MWh)",
        os.path.join(output_dir, "shap_waterfall_agnostic.png"),
    )

    return {
        "shap_values": shap_values,
        "summary_plot": summary_path,
        "waterfall_plot": waterfall_path,
    }


def explain_model_specific(
    model: Any,
    X_train: pd.DataFrame,
    X_test: pd.DataFrame,
    output_dir: str,
) -> dict[str, Any] | None:
    """
    Explains the inner algorithm on the scaled features, in arcsinh space.

    Routes to the exact explainer for the algorithm family; returns None when
    the model has no inner algorithm to pierce (e.g. the naive baseline).
    """
    print("Calculating model-specific SHAP values (arcsinh scale)...")

    scaler, algorithm = _unwrap_pipeline(model)

    if algorithm is None or scaler is None:
        print(
            "  Model has no scaler+algorithm pipeline to pierce; "
            "skipping model-specific SHAP."
        )
        return None

    X_train_scaled = pd.DataFrame(
        scaler.transform(X_train), columns=X_train.columns, index=X_train.index
    )
    X_test_scaled = pd.DataFrame(
        scaler.transform(X_test), columns=X_test.columns, index=X_test.index
    )

    if isinstance(algorithm, xgb.XGBRegressor):
        print("  Detected tree model, using TreeExplainer.")
        explainer = shap.TreeExplainer(algorithm)
    elif isinstance(algorithm, (ElasticNet, LinearRegression, Lasso, Ridge)):
        print("  Detected linear model, using LinearExplainer.")
        explainer = shap.LinearExplainer(algorithm, X_train_scaled)
    else:
        print(f"  Unknown model type {type(algorithm).__name__}, using generic Explainer.")
        with _suppress_shap_rng_warning():
            explainer = shap.Explainer(algorithm, X_train_scaled)

    with _suppress_shap_rng_warning():
        shap_values = explainer(X_test_scaled)

    os.makedirs(output_dir, exist_ok=True)
    summary_path = _save_summary_plot(
        shap_values, X_test_scaled,
        "SHAP Global Feature Importance - Model Specific (arcsinh)",
        os.path.join(output_dir, "shap_summary_specific.png"),
    )
    waterfall_path = _save_waterfall_plot(
        shap_values, 0,
        f"SHAP Contribution Breakdown - {X_test.index[0].date()} (arcsinh)",
        os.path.join(output_dir, "shap_waterfall_specific.png"),
    )

    return {
        "shap_values": shap_values,
        "summary_plot": summary_path,
        "waterfall_plot": waterfall_path,
    }


def explain_predictions_shap(
    model: Any,
    X_train: pd.DataFrame,
    X_test: pd.DataFrame,
    output_dir: str,
    explainer_type: str = "both",
    background_samples: int = 100,
    random_state: int = 42,
) -> dict[str, Any]:
    """
    Generates SHAP attributions for the winning model.

    `explainer_type` is one of 'model_agnostic', 'model_specific' or 'both'.
    """
    valid = ("model_agnostic", "model_specific", "both")
    if explainer_type not in valid:
        raise ValueError(
            f"Unknown explainer_type {explainer_type!r}. Expected one of {list(valid)}."
        )

    print(f"Generating SHAP values using '{explainer_type}' methodology...")
    results: dict[str, Any] = {}

    if explainer_type in ("model_agnostic", "both"):
        results["model_agnostic"] = explain_model_agnostic(
            model, X_train, X_test, output_dir, background_samples, random_state
        )

    if explainer_type in ("model_specific", "both"):
        specific = explain_model_specific(model, X_train, X_test, output_dir)
        if specific is not None:
            results["model_specific"] = specific

    return results


def global_importance_table(shap_values: Any, feature_names: list[str]) -> pd.DataFrame:
    """
    Reduces a SHAP matrix to mean absolute contribution per feature.

    A compact, plot-free ranking that is cheap to log as a table and easy for
    the frontend to render.
    """
    values = np.asarray(shap_values.values)
    mean_abs = np.abs(values).mean(axis=0)

    return (
        pd.DataFrame({
            "feature": feature_names,
            "mean_abs_shap": mean_abs,
        })
        .sort_values("mean_abs_shap", ascending=False)
        .reset_index(drop=True)
    )
