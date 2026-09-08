"""
HTTP client for the forecasting API.

The frontend holds no model and performs no computation — this module is its
only route to inference. Keeping the HTTP details here rather than scattered
through the page means the UI is one client among several possible ones, which
is the point of putting inference behind an API in the first place.

Every call raises `ApiError` carrying the server's own explanation, because the
API already returns actionable messages (a horizon that is too long, a unit
error in nuclear availability) and paraphrasing them would lose information.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import requests
import streamlit as st

DEFAULT_BASE_URL = "http://localhost:8000"

# Generous enough for a cold container loading the model bundle, short enough
# that an unreachable API fails visibly rather than hanging the page.
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 60


def _session() -> requests.Session:
    """
    A session that ignores proxy environment variables.

    The API is reached over a private path — `localhost` locally, the `api`
    service name on the docker network, `127.0.0.1` behind Nginx on EC2. None of
    those should ever traverse an HTTP proxy, but `requests` honours
    `HTTP_PROXY`/`HTTPS_PROXY` by default and will route them there.

    Observed for real: with a proxy exported, `GET http://localhost:8000/health`
    returned **504 Gateway Timeout** from the proxy while the API was healthy and
    answering. `trust_env = False` is the fix, and it is correct rather than a
    workaround — a service-to-service call on a private network has no business
    consulting the ambient proxy configuration.
    """
    session = requests.Session()
    session.trust_env = False
    return session


SESSION = _session()


class ApiError(RuntimeError):
    """A call failed. `detail` carries the server's message when there is one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def base_url() -> str:
    """The API root. Set by compose to the service name on the docker network."""
    return os.getenv("API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _extract_detail(response: requests.Response) -> str:
    """
    Pulls the most useful message out of an error response.

    FastAPI puts a plain string in `detail` for raised HTTPExceptions and a list
    of per-field objects there for Pydantic validation failures. Both shapes are
    unpacked so the user sees the actual reason.
    """
    try:
        payload = response.json()
    except Exception:
        return response.text[:300] or f"HTTP {response.status_code}"

    detail = payload.get("detail", payload) if isinstance(payload, dict) else payload

    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, dict):
                loc = ".".join(str(x) for x in item.get("loc", [])[1:])
                parts.append(f"{loc}: {item.get('msg', '')}".strip(": "))
            else:
                parts.append(str(item))
        return "; ".join(parts) or f"HTTP {response.status_code}"

    if isinstance(detail, dict):
        # /health returns its whole model as the detail when unhealthy
        return str(detail.get("detail") or detail)

    return str(detail)


def _request(method: str, path: str, **kwargs: Any) -> Any:
    url = f"{base_url()}{path}"
    try:
        response = SESSION.request(
            method, url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), **kwargs
        )
    except requests.exceptions.ConnectTimeout:
        raise ApiError(f"Timed out connecting to {url}") from None
    except requests.exceptions.ReadTimeout:
        raise ApiError(f"{url} accepted the request but did not respond in time") from None
    except requests.exceptions.ConnectionError:
        raise ApiError(
            f"Cannot reach the API at {base_url()}. "
            "Is it running? Locally: `docker compose up -d api`."
        ) from None

    if not response.ok:
        raise ApiError(_extract_detail(response), status=response.status_code)

    return response.json()


# --------------------------------------------------------------------- reads
# model_info and backtest_metrics describe a fixed artifact, so they are cached.
# A short TTL means a model promotion is picked up without a page reload.

@st.cache_data(ttl=60, show_spinner=False)
def health() -> dict[str, Any]:
    return _request("GET", "/health")


@st.cache_data(ttl=60, show_spinner=False)
def model_info() -> dict[str, Any]:
    return _request("GET", "/model-info")


@st.cache_data(ttl=300, show_spinner=False)
def backtest_metrics() -> dict[str, Any]:
    return _request("GET", "/backtest-metrics")


# --------------------------------------------------------------------- write
# Deliberately NOT cached: it is the user's action, and caching it would hide
# the fact that the same inputs always produce the same forecast — which is a
# property worth demonstrating, not concealing.

def predict(start_date: date, nuclear_avail: list[float]) -> dict[str, Any]:
    """Requests a forecast. Horizon is implied by the length of the list."""
    return _request(
        "POST",
        "/predict",
        json={
            "start_date": start_date.isoformat(),
            "nuclear_avail": [float(v) for v in nuclear_avail],
        },
    )
