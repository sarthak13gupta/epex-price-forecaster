"""
Request and response contracts for the forecasting API.

Pydantic is doing real work here, not decoration. Nuclear availability is the
single genuine input to the exogenous cascade: a null or a malformed value would
propagate into NaN temperatures, NaN demand and a NaN price that the service
would return with full confidence. Rejecting bad input at the boundary is
therefore a correctness requirement, not a nicety.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Physical bounds for French nuclear availability. Installed capacity is roughly
# 61 GW; the observed range across 2015-2020 is 27.7-61.8 GW. The bounds sit
# deliberately wider than observed so a plausible future value is not rejected,
# while still catching the realistic failure: a GW/MW unit confusion. A floor of
# 1 GW is unreachable for a 61 GW fleet, so anything below it is a unit error
# rather than a scenario. Widen this constant if a total-outage stress test is
# ever genuinely wanted.
NUCLEAR_MIN_MW = 1_000.0
NUCLEAR_MAX_MW = 80_000.0


class ForecastRequest(BaseModel):
    """
    One forecast request.

    The horizon is implied by the length of `nuclear_avail` rather than being a
    separate field. Carrying both would create a redundancy the server has to
    police, and a mismatch between them has no sensible interpretation.
    """

    start_date: date = Field(
        ...,
        description="First day of the forecast horizon (inclusive).",
        examples=["2020-07-01"],
    )
    nuclear_avail: list[float] = Field(
        ...,
        min_length=1,
        description=(
            "Known nuclear availability in MW, one value per forecast day. "
            "Its length determines the horizon."
        ),
        examples=[[29049.0, 29466.0, 30605.0]],
    )

    @field_validator("nuclear_avail")
    @classmethod
    def _check_nuclear_range(cls, values: list[float]) -> list[float]:
        """
        Rejects physically impossible availability.

        A silent unit error is the realistic failure here: 29 (GW) instead of
        29000 (MW) would sail through a null check, produce a wildly negative
        residual demand, and yield a confident nonsense price.
        """
        for i, v in enumerate(values):
            if v != v:  # NaN
                raise ValueError(f"nuclear_avail[{i}] is NaN; a real value is required")
            if not NUCLEAR_MIN_MW <= v <= NUCLEAR_MAX_MW:
                raise ValueError(
                    f"nuclear_avail[{i}] = {v} MW is outside the plausible range "
                    f"[{NUCLEAR_MIN_MW:.0f}, {NUCLEAR_MAX_MW:.0f}] MW. "
                    "Check the units — values are expected in MW, not GW."
                )
        return values


class DailyForecast(BaseModel):
    """
    One forecast day: the price, plus the exogenous state the cascade assumed.

    The drivers are returned deliberately. A forecast that arrives with its
    assumed temperature and demand can be interrogated; a bare number can only
    be trusted or ignored.
    """

    date: date
    predicted_price: float = Field(description="Forecast spot price, EUR/MWh.")
    t_lisse: float = Field(description="Smoothed national temperature, degC.")
    delta_t: float = Field(description="Deviation from the seasonal norm, degC.")
    t_lisse_hdd: float = Field(description="Heating degree days.")
    t_lisse_cdd: float = Field(description="Cooling degree days.")
    demand_mw: float = Field(description="Simulated national demand, MW.")
    nuclear_avail_mw: float = Field(description="Nuclear availability as supplied, MW.")
    residual_demand: float = Field(description="Demand minus nuclear, MW.")


class ForecastResponse(BaseModel):
    """A forecast plus enough provenance to know what produced it."""

    # `model_name` collides with Pydantic's protected `model_` prefix; the
    # namespace is cleared rather than renaming a field the API consumer needs.
    model_config = ConfigDict(protected_namespaces=())

    model_name: str
    train_end: date = Field(description="Last day of observed history behind the model.")
    n_days: int
    mean_predicted_price: float
    forecast: list[DailyForecast]


class ModelInfoResponse(BaseModel):
    """Provenance for the loaded artifact. Answers 'what am I talking to?'."""

    model_config = ConfigDict(protected_namespaces=())

    model_name: str
    model_uri: str
    target_col: str
    n_features: int
    feature_names: list[str]
    train_start: date
    train_end: date
    n_train_days: int
    earliest_forecast_date: date
    max_horizon_days: int
    backtest_mae: float | None
    backtest_rmse: float | None
    best_params: dict[str, float | int | str | bool]


class HealthResponse(BaseModel):
    """Liveness plus readiness. `model_loaded` is the part that matters."""

    model_config = ConfigDict(protected_namespaces=())

    status: str
    model_loaded: bool
    model_uri: str | None = None
    detail: str | None = None
