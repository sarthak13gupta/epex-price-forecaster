"""
FastAPI service for day-ahead French spot price forecasts.

The service holds a fitted `PriceForecaster` in memory and translates HTTP
requests into calls on it. It performs no fitting: all training happens offline
in `src/pipelines/train_pipeline.py`, so two identical requests always return
identical numbers and any served forecast is attributable to a registered model
version.

Run locally:
    uvicorn src.api.main:app --reload --port 8000
Interactive docs:
    http://localhost:8000/docs
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import pandas as pd
from fastapi import FastAPI, HTTPException, status

from src.api.model_loader import LoadedModel, ModelLoadError, load_model
from src.api.schemas import (
    DailyForecast,
    ForecastRequest,
    ForecastResponse,
    HealthResponse,
    ModelInfoResponse,
)
from src.utils.config_loader import load_config

# Process-wide state, populated once during startup. A dict rather than module
# globals so the lifespan handler can replace it atomically.
STATE: dict[str, Any] = {"model": None, "config": None, "load_error": None}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Loads config and the model bundle once, at process start.

    A load failure is captured rather than raised. Crashing on boot gives a
    container restart loop with no way to ask the service what went wrong;
    starting unhealthy keeps /health available to report the reason, while
    /predict refuses with 503 so no traffic is served from a modelless process.
    """
    try:
        config = load_config()
        STATE["config"] = config
        STATE["model"] = load_model(config)
        print(f"[API] Model loaded: {STATE['model'].model_uri}")
    except (ModelLoadError, Exception) as exc:
        STATE["load_error"] = str(exc)
        print(f"[API][ERROR] Model failed to load: {exc}")
    yield
    STATE["model"] = None


app = FastAPI(
    title="French Spot Price Forecaster",
    version="1.0.0",
    summary="Day-ahead EPEX France price forecasts from nuclear availability.",
    description=(
        "Twenty of the model's twenty-two features are unavailable at prediction "
        "time. Only nuclear availability is genuinely known ahead, so the service "
        "manufactures the rest through a four-stage exogenous cascade "
        "(weather -> thermal -> demand -> residual demand) before predicting. "
        "The simulated drivers are returned alongside each price so a forecast "
        "can be interrogated rather than merely trusted."
    ),
    lifespan=lifespan,
)


def _require_model() -> LoadedModel:
    """Returns the loaded model, or refuses the request with 503."""
    model = STATE.get("model")
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "No model is loaded; the service cannot serve forecasts. "
                f"Cause: {STATE.get('load_error') or 'unknown'}"
            ),
        )
    return model


@app.get("/health", response_model=HealthResponse, tags=["ops"])
def health() -> HealthResponse:
    """
    Readiness check that asserts the model actually loaded.

    Deliberately not a bare 200. A service that reports healthy without a model
    is worse than one that is plainly down, because an orchestrator would route
    traffic to it.
    """
    model = STATE.get("model")
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=HealthResponse(
                status="unhealthy",
                model_loaded=False,
                detail=STATE.get("load_error") or "model not loaded",
            ).model_dump(),
        )
    return HealthResponse(status="ok", model_loaded=True, model_uri=model.model_uri)


@app.get("/model-info", response_model=ModelInfoResponse, tags=["ops"])
def model_info() -> ModelInfoResponse:
    """
    Provenance for the loaded artifact.

    Answers "which model produced this number, trained on what, scoring what".
    Sourced from metadata carried inside the artifact rather than from a wiki,
    so it cannot drift away from the model it describes.
    """
    model = _require_model()
    meta = model.forecaster.metadata.to_dict()
    return ModelInfoResponse(
        model_name=meta["model_name"],
        model_uri=model.model_uri,
        target_col=meta["target_col"],
        n_features=meta["n_features"],
        feature_names=meta["feature_names"],
        train_start=pd.Timestamp(meta["train_start"]).date(),
        train_end=pd.Timestamp(meta["train_end"]).date(),
        n_train_days=meta["n_train_days"],
        earliest_forecast_date=model.forecaster.earliest_forecast_date.date(),
        max_horizon_days=meta["max_horizon_days"],
        backtest_mae=meta["backtest_mae"],
        backtest_rmse=meta["backtest_rmse"],
        best_params=meta["best_params"],
    )


@app.post("/predict", response_model=ForecastResponse, tags=["forecast"])
def predict(request: ForecastRequest) -> ForecastResponse:
    """
    Forecasts the spot price path over the requested horizon.

    Pydantic has already established that the request is well-formed and that
    nuclear availability is physically plausible. What remains are semantic
    checks the model itself owns — horizon length, and whether the start date is
    forecastable given the training window — so those errors are surfaced from
    the forecaster rather than duplicated here.
    """
    model = _require_model()
    config = STATE["config"]
    nuclear_col = config["feature_engineering"]["nuclear_col"]
    demand_col = config["feature_engineering"]["demand_col"]

    horizon = len(request.nuclear_avail)
    index = pd.date_range(request.start_date, periods=horizon, freq="D")
    frame = pd.DataFrame({nuclear_col: request.nuclear_avail}, index=index)
    frame.index.name = "Date"

    try:
        result = model.forecaster.forecast(frame)
    except (ValueError, KeyError) as exc:
        # The request parsed correctly but is not one this model can answer:
        # too long a horizon, or a start date inside the training window. The
        # forecaster's messages explain why, so they are passed through.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    days = [
        DailyForecast(
            date=ts.date(),
            predicted_price=float(row["predicted_price"]),
            t_lisse=float(row["T_lisse"]),
            delta_t=float(row["Delta_T"]),
            t_lisse_hdd=float(row["T_lisse_HDD"]),
            t_lisse_cdd=float(row["T_lisse_CDD"]),
            demand_mw=float(row[demand_col]),
            nuclear_avail_mw=float(row[nuclear_col]),
            residual_demand=float(row["Residual_Demand"]),
        )
        for ts, row in result.iterrows()
    ]

    return ForecastResponse(
        model_name=model.forecaster.metadata.model_name,
        train_end=model.forecaster.train_end.date(),
        n_days=len(days),
        mean_predicted_price=float(result["predicted_price"].mean()),
        forecast=days,
    )


@app.get("/backtest-metrics", tags=["forecast"])
def backtest_metrics() -> dict[str, Any]:
    """
    Returns the walk-forward leaderboard and per-regime breakdown.

    Served from the training run's artifacts rather than recomputed: a backtest
    is a training-time concern and re-running one inside a request would take
    minutes. Exposing it lets the frontend show how the model was validated,
    not just what it predicts.
    """
    _require_model()
    output_dir = STATE["config"]["paths"]["output_dir"]

    payload: dict[str, Any] = {}
    for key, filename in (
        ("leaderboard", "leaderboard.csv"),
        ("by_regime", "metrics_by_regime.csv"),
    ):
        path = os.path.join(output_dir, filename)
        if not os.path.exists(path):
            continue
        payload[key] = pd.read_csv(path).to_dict(orient="records")

    if not payload:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No backtest artifacts found. Run the training pipeline to "
                "generate them."
            ),
        )
    return payload
