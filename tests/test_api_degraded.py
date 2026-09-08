"""
The modelless path. CI has no MLflow registry, so the API boots with no model
loaded — which is exactly the condition worth asserting, because the design
choice was deliberate: a load failure is *captured*, not raised, so the service
starts unhealthy and can be asked why, instead of crash-looping silently.

These tests force that state explicitly rather than relying on CI's environment,
so they behave the same on a developer machine that does have a registry.
"""
import pytest
from fastapi.testclient import TestClient

from src.api import main


@pytest.fixture
def modelless_client(monkeypatch) -> TestClient:
    """An app instance whose model load failed with a known message."""
    monkeypatch.setitem(main.STATE, "model", None)
    monkeypatch.setitem(main.STATE, "load_error", "no registry in this environment")

    # TestClient runs the lifespan, which would overwrite STATE, so neutralise
    # the loader itself and let the captured error stand.
    def _fail(_config):
        raise RuntimeError("no registry in this environment")

    monkeypatch.setattr(main, "load_model", _fail)
    with TestClient(main.app) as client:
        yield client


def test_health_reports_unhealthy_rather_than_a_bare_200(modelless_client):
    """A 200 with no model would make an orchestrator route live traffic here."""
    response = modelless_client.get("/health")
    assert response.status_code == 503
    assert "no registry" in response.text


def test_predict_refuses_instead_of_serving_nonsense(modelless_client):
    response = modelless_client.post(
        "/predict",
        json={"start_date": "2020-07-01", "nuclear_avail": [30000.0, 30500.0]},
    )
    assert response.status_code == 503
    assert "cannot serve forecasts" in response.json()["detail"]


def test_model_info_refuses_when_there_is_no_model(modelless_client):
    assert modelless_client.get("/model-info").status_code == 503


def test_validation_runs_before_the_model_is_consulted(modelless_client):
    """
    A malformed request must get 422, not 503 — the client needs to know its
    payload is wrong regardless of server state.
    """
    response = modelless_client.post(
        "/predict",
        json={"start_date": "2020-07-01", "nuclear_avail": [29.0]},
    )
    assert response.status_code == 422
    assert "not GW" in response.text


def test_openapi_schema_is_generated(modelless_client):
    """Catches response-model misconfiguration, which only surfaces on render."""
    schema = modelless_client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/predict" in schema.json()["paths"]
