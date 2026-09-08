"""
Resolves and loads the served model exactly once per process.

Loading is deliberately a startup concern, not a request concern. The bundle
crosses from MLflow into this process at boot and is then held in memory, which
keeps MLflow a *startup* dependency rather than a *runtime* one: a running API
survives the tracking server being unavailable.
"""

from __future__ import annotations

import os
from typing import Any

import mlflow
from mlflow import MlflowClient

from src.models.forecaster import PriceForecaster
from src.utils.config_loader import load_config

# Alias the API prefers when no explicit URI is given. Loading by alias rather
# than by version number is what makes promotion a registry operation plus a
# restart, instead of a code change and a rebuild.
CHAMPION_ALIAS = "champion"


class ModelLoadError(RuntimeError):
    """Raised when no servable model can be resolved."""


def resolve_model_uri(config: dict[str, Any]) -> str:
    """
    Picks the model URI to serve, in descending order of explicitness.

    1. `MODEL_URI` environment variable — an explicit operator override, used to
       pin a specific version or to serve a run-scoped model during debugging.
    2. The `champion` alias — the intended production path.
    3. The highest registered version — a fallback so a freshly trained project
       serves something without requiring an alias to have been set yet.

    Raising rather than silently serving nothing is the point: an API that boots
    without a model is worse than one that refuses to boot.
    """
    override = os.getenv("MODEL_URI")
    if override:
        return override

    name = config["mlflow"]["registered_model_name"]
    client = MlflowClient()

    try:
        client.get_model_version_by_alias(name, CHAMPION_ALIAS)
        return f"models:/{name}@{CHAMPION_ALIAS}"
    except Exception:
        # No alias set yet. Fall back to the newest version rather than failing,
        # but the alias remains the path that should be used in deployment.
        pass

    try:
        versions = client.search_model_versions(f"name='{name}'")
    except Exception as exc:
        raise ModelLoadError(
            f"Could not query the MLflow registry for {name!r}: {exc}"
        ) from exc

    if not versions:
        raise ModelLoadError(
            f"No registered versions found for model {name!r}. "
            "Run `python -m src.pipelines.train_pipeline` to train and register one."
        )

    latest = max(versions, key=lambda mv: int(mv.version))
    return f"models:/{name}/{latest.version}"


class LoadedModel:
    """The served artifact plus the URI it came from."""

    def __init__(self, forecaster: PriceForecaster, model_uri: str) -> None:
        self.forecaster = forecaster
        self.model_uri = model_uri


def load_model(config: dict[str, Any] | None = None) -> LoadedModel:
    """
    Loads the forecaster bundle from MLflow.

    Returns the inner `PriceForecaster` rather than the pyfunc wrapper. The
    wrapper exists to give MLflow a DataFrame-in/DataFrame-out interface; inside
    this process the richer typed object is more useful, and the API layer does
    its own request translation.
    """
    config = config or load_config()
    mlflow.set_tracking_uri(config["mlflow"]["tracking_uri"])

    model_uri = resolve_model_uri(config)

    try:
        pyfunc_model = mlflow.pyfunc.load_model(model_uri)
    except Exception as exc:
        raise ModelLoadError(f"Failed to load model from {model_uri!r}: {exc}") from exc

    forecaster = getattr(pyfunc_model.unwrap_python_model(), "forecaster", None)
    if forecaster is None:
        raise ModelLoadError(
            f"Model at {model_uri!r} carries no PriceForecaster. It was probably "
            "logged by an older pipeline version."
        )

    return LoadedModel(forecaster=forecaster, model_uri=model_uri)
