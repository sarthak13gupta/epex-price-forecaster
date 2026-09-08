"""
An import smoke test. Cheap, and it catches the class of breakage that only
appears in a clean environment: a module that imports something the service's
slim requirements file does not install. CI runs this against the API
requirement set, so a training-only dependency leaking into src/api would fail
here rather than at container start.
"""
import importlib

import pytest

API_MODULES = [
    "src.api.main",
    "src.api.model_loader",
    "src.api.schemas",
    "src.data.data_loader",
    "src.data.preprocess",
    "src.data.s3_store",
    "src.data.schema",
    "src.features.build_features",
    "src.models.backtest",
    "src.models.forecaster",
    "src.models.registry",
    "src.models.simulate_exogenous",
    "src.utils.config_loader",
]

TRAIN_MODULES = ["src.models.train_tune", "src.pipelines.train_pipeline"]

UI_MODULES = ["src.ui.api_client", "src.ui.charts", "src.ui.theme"]


@pytest.mark.parametrize("module", API_MODULES)
def test_api_module_imports(module):
    importlib.import_module(module)


@pytest.mark.parametrize("module", TRAIN_MODULES)
def test_training_module_imports(module):
    pytest.importorskip("optuna", reason="training extras not installed")
    importlib.import_module(module)


@pytest.mark.parametrize("module", UI_MODULES)
def test_ui_module_imports(module):
    pytest.importorskip("streamlit", reason="UI extras not installed")
    importlib.import_module(module)


def test_fastapi_app_exposes_the_documented_routes():
    from src.api.main import app

    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    for expected in ("/health", "/model-info", "/predict", "/backtest-metrics"):
        assert expected in paths, f"{expected} is missing from the API"
