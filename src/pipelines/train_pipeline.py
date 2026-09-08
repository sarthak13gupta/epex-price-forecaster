"""
End-to-end training pipeline.

    ingest -> preprocess -> static features -> prepare folds
           -> tune + backtest every enabled model
           -> leaderboard -> fit the winner on the latest window
           -> SHAP -> log everything to MLflow -> register the model

Run with:  python -m src.pipelines.train_pipeline
"""

import argparse
import json
import os
import traceback
from typing import Any

import mlflow
import pandas as pd
from mlflow.models import infer_signature

from src.analysis.evaluate import (
    build_leaderboard,
    metrics_to_mlflow_dict,
    per_regime_metrics_to_mlflow_dict,
    plot_backtest_predictions,
    plot_forecast,
    plot_residual_diagnostics,
    print_leaderboard,
    select_best_model,
    summarize_by_regime,
)
from src.analysis.explainability import (
    explain_predictions_shap,
    global_importance_table,
)
from src.data.data_loader import load_raw_dataset
from src.data.preprocess import preprocess_data, prepare_future_exogenous
from src.data.s3_store import S3Store
from src.features.build_features import build_static_features, drop_incomplete_rows
from src.models.backtest import evaluate_folds, prepare_folds
from src.models.forecaster import PriceForecaster, PriceForecasterModel
from src.models.registry import build_model_registry
from src.models.train_tune import tune_model
from src.utils.config_loader import (
    get_active_schedule,
    get_feature_names,
    get_hdd_cdd_columns,
    load_config,
)


def _ensure_experiment(config: dict[str, Any]) -> str:
    """
    Selects the experiment, creating it with an explicit artifact location.

    Without a pinned location a SQLite-backed store writes artifacts relative to
    the launching process's working directory, which scatters them once the API
    and the training job run from different places.
    """
    name = config["mlflow"]["experiment_name"]
    existing = mlflow.get_experiment_by_name(name)

    if existing is None:
        artifact_location = config["mlflow"].get("artifact_location")
        if artifact_location:
            os.makedirs(artifact_location, exist_ok=True)
        mlflow.create_experiment(name, artifact_location=artifact_location)

    mlflow.set_experiment(name)
    return name


def _slice_holdout(future_exog: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    """
    Restricts the holdout forecast to the configured window.

    Both bounds are optional: with neither set, the whole prediction file is
    forecast, which is the behaviour a live daily feed wants.
    """
    forecast_config = config["forecast"]
    start = forecast_config.get("holdout_start")
    end = forecast_config.get("holdout_end")

    if start is None and end is None:
        return future_exog

    index = pd.DatetimeIndex(future_exog.index)
    mask = pd.Series(True, index=future_exog.index)

    if start is not None:
        mask &= index >= pd.Timestamp(start)
    if end is not None:
        mask &= index <= pd.Timestamp(end)

    sliced = future_exog[mask]

    if sliced.empty:
        raise ValueError(
            f"Holdout window {start}..{end} selects no rows from the prediction "
            f"file, which covers "
            f"{future_exog.index[0].date()}..{future_exog.index[-1].date()}."
        )

    return sliced


def _log_dataframe_artifact(df: pd.DataFrame, output_dir: str, filename: str) -> str:
    """Writes a DataFrame to CSV and returns the path, for MLflow logging."""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, filename)
    df.to_csv(path, index=True)
    return path


def run_training_pipeline(
    config: dict[str, Any] | None = None,
    skip_tuning: bool = False,
    skip_shap: bool = False,
    register: bool = True,
) -> dict[str, Any]:
    """
    Trains, benchmarks, selects, fits and registers the price forecaster.

    Returns a summary dict with the leaderboard, the winning model name and the
    MLflow run/model identifiers.
    """
    config = config or load_config()

    target_col = config["dataset"]["target"]
    feature_names = get_feature_names(config)
    hdd_cdd_cols = get_hdd_cdd_columns(config)
    calendar_features = list(config["features"]["calendar"])
    schedule = get_active_schedule(config)
    selection_metric = config["models"]["selection_metric"]
    output_dir = config["paths"]["output_dir"]

    os.makedirs(output_dir, exist_ok=True)

    mlflow.set_tracking_uri(config["mlflow"]["tracking_uri"])
    _ensure_experiment(config)

    print("=" * 60)
    print("STAGE 1/6 — Data ingestion and preprocessing")
    print("=" * 60)
    raw_df = load_raw_dataset(dataset_type="train")
    clean_df = preprocess_data(raw_df, config)

    print("\n" + "=" * 60)
    print("STAGE 2/6 — Static feature engineering")
    print("=" * 60)
    featured_df = build_static_features(clean_df, config)
    featured_df = drop_incomplete_rows(featured_df, config)
    print(f"Feature matrix ready: {featured_df.shape[0]} days x {len(feature_names)} model features")

    print("\n" + "=" * 60)
    print("STAGE 3/6 — Walk-forward fold preparation")
    print("=" * 60)
    # Prepared once and shared by every model and every tuning trial: the
    # cascade depends on the training window, never on hyperparameters.
    folds = prepare_folds(
        df=featured_df,
        target_col=target_col,
        features=feature_names,
        schedule=schedule,
        calendar_features=calendar_features,
        hdd_cdd_cols=hdd_cdd_cols,
        config=config,
    )

    print("\n" + "=" * 60)
    print("STAGE 4/6 — Benchmarking the model registry")
    print("=" * 60)
    registry = build_model_registry(config)

    all_metrics: list[pd.DataFrame] = []
    all_results: list[pd.DataFrame] = []
    selected_estimators: dict[str, Any] = {}
    best_params: dict[str, dict[str, Any]] = {}

    parent_run_name = f"training_{pd.Timestamp.now():%Y%m%d_%H%M%S}"

    with mlflow.start_run(run_name=parent_run_name) as parent_run:
        mlflow.log_params({
            "active_schedule": config["backtest"]["active_schedule"],
            "n_folds": len(folds),
            "train_window_days": config["backtest"]["train_window_days"],
            "warmup_days": config["backtest"]["warmup_days"],
            "feature_set": config["features"]["feature_set"],
            "n_features": len(feature_names),
            "selection_metric": selection_metric,
            "target": target_col,
            "hdd_cdd_reference": ",".join(config["feature_engineering"]["hdd_cdd_reference"]),
        })
        mlflow.set_tag("stage", "training")

        for model_name, spec in registry.items():
            print(f"\n--- {model_name} ---")

            with mlflow.start_run(run_name=model_name, nested=True):
                estimator = spec.estimator

                if spec.requires_tuning and not skip_tuning:
                    assert spec.param_space is not None
                    estimator, study = tune_model(
                        folds=folds,
                        base_estimator=estimator,
                        param_space=spec.param_space,
                        model_name=model_name,
                        config=config,
                    )
                    mlflow.log_params(study.best_params)
                    mlflow.log_metrics({
                        "tuning_best_rmse": float(study.best_value),
                        "tuning_n_trials": len(study.trials),
                    })
                    best_params[model_name] = dict(study.best_params)
                elif spec.requires_tuning:
                    print(f"Skipping tuning for {model_name}, using defaults.")

                metrics_df, results_df = evaluate_folds(
                    folds, estimator, model_name=model_name
                )

                mlflow.log_metrics({
                    "mae": float(metrics_df["mae"].mean()),
                    "rmse": float(metrics_df["rmse"].mean()),
                    "mae_std": float(metrics_df["mae"].std()),
                    "worst_fold_mae": float(metrics_df["mae"].max()),
                })
                mlflow.log_metrics(
                    per_regime_metrics_to_mlflow_dict(metrics_df, model_name)
                )
                mlflow.log_artifact(
                    _log_dataframe_artifact(
                        metrics_df, output_dir, f"fold_metrics_{model_name}.csv"
                    )
                )

                all_metrics.append(metrics_df)
                all_results.append(results_df)
                selected_estimators[model_name] = estimator

        master_metrics = pd.concat(all_metrics, ignore_index=True)
        master_results = pd.concat(all_results)

        print("\n" + "=" * 60)
        print("STAGE 5/6 — Leaderboard and model selection")
        print("=" * 60)
        leaderboard = build_leaderboard(master_metrics, selection_metric)
        print_leaderboard(leaderboard, selection_metric)

        regime_table = summarize_by_regime(master_metrics)
        print("\nMean MAE by regime:")
        print(regime_table.to_string())

        best_model_name = select_best_model(leaderboard, selection_metric)
        best_row = leaderboard[leaderboard["model"] == best_model_name].iloc[0]
        print(f"\nSelected model: {best_model_name}")

        mlflow.log_param("best_model", best_model_name)
        mlflow.log_metrics(metrics_to_mlflow_dict(best_row, prefix="best_"))
        mlflow.log_artifact(
            _log_dataframe_artifact(leaderboard, output_dir, "leaderboard.csv")
        )
        mlflow.log_artifact(
            _log_dataframe_artifact(regime_table, output_dir, "metrics_by_regime.csv")
        )
        mlflow.log_artifact(
            _log_dataframe_artifact(
                master_results, output_dir, "backtest_predictions.csv"
            )
        )
        mlflow.log_artifact(
            plot_residual_diagnostics(master_results, best_model_name, output_dir)
        )
        mlflow.log_artifact(
            plot_backtest_predictions(master_results, best_model_name, output_dir)
        )

        print("\n" + "=" * 60)
        print("STAGE 6/6 — Fitting the winner and registering the artifact")
        print("=" * 60)

        # Fit on the most recent window so the served model is as current as the
        # data allows, using the same window length the backtest validated.
        train_window_days = int(config["backtest"]["train_window_days"])
        latest_end = pd.Timestamp(featured_df.index[-1])
        latest_start = latest_end - pd.Timedelta(train_window_days - 1, unit="D")
        final_train = featured_df.loc[latest_start:latest_end]

        print(
            f"Final fit window: {latest_start.date()} .. {latest_end.date()} "
            f"({len(final_train)} days)"
        )

        forecaster = PriceForecaster.fit(
            train_data=final_train,
            price_model=selected_estimators[best_model_name],
            model_name=best_model_name,
            config=config,
            feature_names=feature_names,
            calendar_features=calendar_features,
            hdd_cdd_cols=hdd_cdd_cols,
            backtest_mae=float(best_row["mae"]),
            backtest_rmse=float(best_row["rmse"]),
            best_params=best_params.get(best_model_name),
        )

        metadata_path = os.path.join(output_dir, "model_metadata.json")
        with open(metadata_path, "w") as handle:
            json.dump(forecaster.metadata.to_dict(), handle, indent=2)
        mlflow.log_artifact(metadata_path)

        # Forward forecast over the holdout window, which exercises the exact
        # code path the API will use.
        holdout_forecast = None
        try:
            pred_raw = load_raw_dataset(dataset_type="predict")
            future_exog = prepare_future_exogenous(pred_raw, config)
            future_exog = _slice_holdout(future_exog, config)
            holdout_forecast = forecaster.forecast(future_exog)

            print(
                f"\nHoldout forecast: {len(holdout_forecast)} days, "
                f"mean {holdout_forecast['predicted_price'].mean():.2f} EUR/MWh "
                f"(range {holdout_forecast['predicted_price'].min():.2f} .. "
                f"{holdout_forecast['predicted_price'].max():.2f})"
            )
            mlflow.log_metric(
                "holdout_mean_predicted_price",
                float(holdout_forecast["predicted_price"].mean()),
            )
            mlflow.log_artifact(
                _log_dataframe_artifact(
                    holdout_forecast, output_dir, "holdout_forecast.csv"
                )
            )
            mlflow.log_artifact(plot_forecast(holdout_forecast, output_dir))

            # Archive the forecast to its date partition in S3. No-op when
            # ENV != production. This cannot be reconstructed retrospectively:
            # the realised-accuracy record can only start once it exists.
            S3Store(config).save_forecast(holdout_forecast)
        except (FileNotFoundError, ValueError, KeyError) as exc:
            # A missing or malformed holdout file must not fail a training run
            # whose model is otherwise sound.
            print(f"[WARN] Skipping holdout forecast: {exc}")

        if not skip_shap and config["explainability"]["enabled"]:
            print("\nGenerating SHAP attributions for the selected model...")
            try:
                X_train_shap = forecaster.training_design_matrix
                X_test_shap = (
                    forecaster.design_matrix(future_exog)
                    if holdout_forecast is not None
                    else folds[-1].X_test
                )
                shap_results = explain_predictions_shap(
                    model=forecaster.price_model,
                    X_train=X_train_shap,
                    X_test=X_test_shap,
                    output_dir=output_dir,
                    explainer_type="both",
                    background_samples=int(
                        config["explainability"]["background_samples"]
                    ),
                    random_state=int(config["models"]["random_state"]),
                )
                for view in shap_results.values():
                    mlflow.log_artifact(view["summary_plot"])
                    mlflow.log_artifact(view["waterfall_plot"])

                if "model_agnostic" in shap_results:
                    importance = global_importance_table(
                        shap_results["model_agnostic"]["shap_values"],
                        list(X_test_shap.columns),
                    )
                    print("\nTop 10 features by mean |SHAP| (EUR/MWh):")
                    print(importance.head(10).to_string(index=False))
                    mlflow.log_artifact(
                        _log_dataframe_artifact(
                            importance, output_dir, "shap_importance.csv"
                        )
                    )
            except Exception as exc:
                # SHAP is diagnostic, not load-bearing: a failure here must not
                # cost us a trained and validated model. Print the traceback
                # rather than just the message, so the cause is actionable.
                print(f"[WARN] SHAP generation failed: {exc}")
                traceback.print_exc()

        # Log the bundle as a pyfunc so FastAPI can load it by URI. code_paths
        # ships src/ with the artifact, so the loading process does not need the
        # repo on its PYTHONPATH.
        pyfunc_model = PriceForecasterModel(forecaster)

        signature = None
        example_input = None
        if holdout_forecast is not None:
            example_input = pd.DataFrame({
                PriceForecasterModel.DATE_INPUT_COLUMN:
                    holdout_forecast.index.strftime("%Y-%m-%d"),
                PriceForecasterModel.NUCLEAR_INPUT_COLUMN:
                    holdout_forecast[
                        config["feature_engineering"]["nuclear_col"]
                    ].to_numpy(),
            })
            signature = infer_signature(
                example_input, holdout_forecast.reset_index()
            )

        registered_name = (
            config["mlflow"]["registered_model_name"] if register else None
        )

        logged = mlflow.pyfunc.log_model(
            name="price_forecaster",
            python_model=pyfunc_model,
            code_paths=["src"],
            signature=signature,
            input_example=example_input,
            registered_model_name=registered_name,
        )

        print(f"\nLogged model URI: {logged.model_uri}")
        if registered_name:
            print(f"Registered as: {registered_name}")

        run_id = parent_run.info.run_id

    print("\n" + "=" * 60)
    print("TRAINING PIPELINE COMPLETE")
    print("=" * 60)
    print(f"MLflow run: {run_id}")
    print(f"Tracking URI: {config['mlflow']['tracking_uri']}")

    return {
        "run_id": run_id,
        "model_uri": logged.model_uri,
        "registered_model_name": registered_name,
        "best_model": best_model_name,
        "leaderboard": leaderboard,
        "metrics": master_metrics,
        "results": master_results,
        "forecaster": forecaster,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train, benchmark and register the French spot price forecaster."
    )
    parser.add_argument(
        "--skip-tuning",
        action="store_true",
        help="Benchmark with default hyperparameters. Fast smoke-test path.",
    )
    parser.add_argument(
        "--skip-shap",
        action="store_true",
        help="Skip SHAP attribution, which dominates runtime on the agnostic explainer.",
    )
    parser.add_argument(
        "--no-register",
        action="store_true",
        help="Log the model to the run without adding it to the MLflow registry.",
    )
    args = parser.parse_args()

    run_training_pipeline(
        skip_tuning=args.skip_tuning,
        skip_shap=args.skip_shap,
        register=not args.no_register,
    )


if __name__ == "__main__":
    main()
