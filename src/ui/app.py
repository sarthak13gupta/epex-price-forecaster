"""
Streamlit frontend for the French spot price forecaster.

Presentation only. It holds no model, loads no artifact and performs no
computation — every number on screen came from the FastAPI service over HTTP.
That separation is what lets the API stay reusable by non-human callers and
keeps a UI bug from touching inference.

Run:
    streamlit run src/ui/app.py
Reads API_BASE_URL (compose sets it to http://api:8000).
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from src.ui import api_client as api
from src.ui.charts import DRIVERS, driver_panels, leaderboard_bars, price_path, regime_bars
from src.ui.theme import palette

st.set_page_config(
    page_title="French Spot Price Forecaster",
    page_icon="⚡",
    layout="wide",
)

PAL = palette()

# Fallbacks used only when /model-info is unreachable, so the controls still
# render with sane bounds instead of the page dying.
FALLBACK_MAX_HORIZON = 31
FALLBACK_START = date(2020, 7, 1)
NUCLEAR_MIN, NUCLEAR_MAX = 1_000, 80_000


# ============================================================== connection

def connection_panel() -> dict | None:
    """
    Sidebar status. Returns /model-info when the API is serving, else None.

    Failures are shown with the server's own message rather than a traceback:
    the API already explains why it is unhealthy (a missing model, a bad URI),
    and that explanation is more useful than anything this layer could invent.
    """
    with st.sidebar:
        st.caption(f"API · `{api.base_url()}`")
        try:
            health = api.health()
        except api.ApiError as exc:
            st.error("API unreachable")
            st.caption(str(exc))
            return None

        if not health.get("model_loaded"):
            st.warning("API is up but no model is loaded")
            st.caption(health.get("detail") or "unknown cause")
            return None

        st.success("Serving")
        try:
            info = api.model_info()
        except api.ApiError as exc:
            st.error(f"/model-info failed: {exc}")
            return None

        st.caption(
            f"**{info['model_name']}** · backtest MAE "
            f"{info['backtest_mae']:.2f} EUR/MWh"
        )
        st.caption(f"trained through {info['train_end']}")
        return info


# ============================================================== forecast tab

def nuclear_inputs(horizon: int, index: pd.DatetimeIndex) -> list[float]:
    """
    Collects nuclear availability, the only genuinely known input.

    A flat baseline plus a per-day editor: a constant is what a user reaches for
    first, and outages are what makes the input interesting. The slider is
    bounded to the API's own accepted range so the common path cannot produce a
    422, while the editor still allows one — the validation is worth seeing work.
    """
    st.markdown("**Nuclear availability** — the one input that is known ahead")
    st.caption(
        "Published as a maintenance schedule. Everything else the model needs "
        "is simulated from it."
    )

    baseline = st.slider(
        "Baseline across the horizon (MW)",
        min_value=NUCLEAR_MIN, max_value=NUCLEAR_MAX,
        value=30_000, step=500,
        help=f"The API accepts {NUCLEAR_MIN:,}–{NUCLEAR_MAX:,} MW. Values below "
             "1 GW are rejected as a GW/MW unit error.",
    )

    with st.expander("Vary individual days (simulate an outage)"):
        edited = st.data_editor(
            pd.DataFrame({
                "date": index.date,
                "nuclear_avail_mw": [float(baseline)] * horizon,
            }),
            hide_index=True, width='stretch',
            disabled=["date"],
            column_config={
                "nuclear_avail_mw": st.column_config.NumberColumn(
                    "Nuclear (MW)", min_value=0, max_value=200_000, step=500,
                    format="%d",
                )
            },
            key=f"nuclear_editor_{horizon}",
        )
    return edited["nuclear_avail_mw"].tolist()


def forecast_tab(info: dict) -> None:
    max_horizon = info.get("max_horizon_days", FALLBACK_MAX_HORIZON)
    earliest = pd.Timestamp(
        info.get("earliest_forecast_date", FALLBACK_START)
    ).date()

    left, right = st.columns([1, 2], gap="large")

    with left:
        st.subheader("Request")
        start = st.date_input(
            "First forecast day", value=earliest, min_value=earliest,
            help=f"The model was trained through {info['train_end']}, so this is "
                 "the earliest day it can honestly forecast.",
        )
        horizon = st.slider(
            "Horizon (days)", 1, max_horizon, min(7, max_horizon),
            help=f"Capped at {max_horizon} days: the weather model's AR(2) term "
                 "decays to its seasonal mean well before that.",
        )
        index = pd.date_range(start, periods=horizon, freq="D")
        nuclear = nuclear_inputs(horizon, index)
        go_pressed = st.button("Forecast", type="primary", width='stretch')

    with right:
        if not go_pressed:
            st.info(
                "Set a start date and horizon, then press **Forecast**.\n\n"
                "The response returns the simulated drivers alongside the price, "
                "so the forecast can be interrogated rather than merely trusted."
            )
            return

        try:
            with st.spinner("Running the exogenous cascade…"):
                result = api.predict(start, nuclear)
        except api.ApiError as exc:
            st.error(f"Request refused ({exc.status or 'error'})")
            st.caption(str(exc))
            return

        frame = pd.DataFrame(result["forecast"])
        frame["date"] = pd.to_datetime(frame["date"])

        prices = frame["predicted_price"]
        a, b, c = st.columns(3)
        a.metric("Mean forecast", f"{result['mean_predicted_price']:.2f}",
                 help="EUR/MWh across the horizon")
        b.metric("Range", f"{prices.min():.1f} – {prices.max():.1f}")
        c.metric("Days", result["n_days"])

        st.markdown(f"#### Forecast price · {result['model_name']}")
        st.plotly_chart(price_path(frame, PAL), width='stretch',
                        theme=None, key="price_chart")

        st.markdown("#### What the cascade assumed")
        st.caption(
            "Separate panels sharing one x-axis, not a dual-axis overlay — "
            "degrees and megawatts do not belong on one scale."
        )
        st.plotly_chart(driver_panels(frame, PAL), width='stretch',
                        theme=None, key="driver_chart")

        with st.expander("Table view"):
            st.dataframe(
                frame.assign(date=frame["date"].dt.date), hide_index=True,
                width='stretch',
            )
            st.download_button(
                "Download CSV", frame.to_csv(index=False).encode(),
                file_name=f"forecast_{start}.csv", mime="text/csv",
            )


# ============================================================== model tab

def model_tab(info: dict) -> None:
    st.subheader("What is serving")
    st.caption(
        "Read from metadata carried inside the model artifact, not from a wiki — "
        "so it cannot drift away from the model it describes."
    )

    a, b = st.columns(2)
    with a:
        st.markdown(f"""
| | |
|---|---|
| **Model** | `{info['model_name']}` |
| **Registry URI** | `{info['model_uri']}` |
| **Target** | `{info['target_col']}` |
| **Features** | {info['n_features']} |
""")
    with b:
        mae = info.get("backtest_mae")
        rmse = info.get("backtest_rmse")
        st.markdown(f"""
| | |
|---|---|
| **Trained** | {info['train_start']} → {info['train_end']} |
| **Window** | {info['n_train_days']} days |
| **Earliest forecast** | {info['earliest_forecast_date']} |
| **Max horizon** | {info['max_horizon_days']} days |
| **Backtest MAE / RMSE** | {mae:.3f} / {rmse:.3f} |
""")

    st.warning(
        "**8.68 EUR/MWh is a warm-season figure.** The active backtest schedule "
        "covers May–September only, so this is not an annual accuracy number."
    )

    with st.expander(f"Design matrix — {info['n_features']} features, in model order"):
        names = info["feature_names"]
        st.caption(
            "Order matters: it is baked into the fitted model, so the API must "
            "present columns in exactly this sequence."
        )
        st.dataframe(
            pd.DataFrame({"#": range(1, len(names) + 1), "feature": names}),
            hide_index=True, width='stretch', height=320,
        )

    if info.get("best_params"):
        with st.expander("Tuned hyperparameters"):
            st.json(info["best_params"])


# ============================================================== validation tab

def validation_tab() -> None:
    st.subheader("How the model was validated")
    st.caption(
        "Walk-forward, 12 folds, one calendar month each, trained on the 731 days "
        "immediately before it. The whole exogenous cascade refits per fold, so a "
        "fold's test features are simulated from that fold's training window only."
    )

    try:
        metrics = api.backtest_metrics()
    except api.ApiError as exc:
        st.error(f"Backtest metrics unavailable: {exc}")
        st.caption(
            "They are served from the training run's artifacts. Run the pipeline "
            "to generate them."
        )
        return

    board = metrics.get("leaderboard", [])
    if board:
        st.markdown("#### Leaderboard")
        metric = st.radio(
            "Metric", ["mae", "rmse"], horizontal=True, label_visibility="collapsed",
            format_func=lambda m: {"mae": "Mean MAE", "rmse": "Mean RMSE"}[m],
        )
        st.plotly_chart(leaderboard_bars(board, PAL, metric),
                        width='stretch', theme=None, key="board_chart")
        st.caption(
            "A naive seasonal baseline is kept in the registry deliberately: "
            "without it, an error figure has no scale."
        )
        with st.expander("Table view"):
            st.dataframe(pd.DataFrame(board).round(3), hide_index=True,
                         width='stretch')

    regimes = metrics.get("by_regime", [])
    if regimes:
        st.markdown("#### Error by market regime")
        st.caption(
            "The reason to report folds individually: a single mean hides where a "
            "model fails. Note August, where every model is at its worst — and "
            "Lockdown Easing, where the naive baseline wins."
        )
        st.plotly_chart(regime_bars(regimes, PAL), width='stretch',
                        theme=None, key="regime_chart")
        with st.expander("Table view"):
            st.dataframe(pd.DataFrame(regimes), hide_index=True,
                         width='stretch')


# ============================================================== entry point

def main() -> None:
    st.title("⚡ French Day-Ahead Spot Price Forecaster")
    st.caption(
        "Twenty of the model's twenty-two features do not exist at prediction "
        "time. Only nuclear availability is known ahead — the rest are "
        "manufactured by a four-stage cascade before the price model runs."
    )

    info = connection_panel()

    if info is None:
        st.error("Not connected to a serving API — nothing can be forecast.")
        st.markdown(
            f"""
The frontend holds no model by design; it needs the API.

```bash
export HOST_PROJECT_DIR=$PWD HOST_UID=$(id -u) HOST_GID=$(id -g)
docker compose up -d api
```

Currently pointing at `{api.base_url()}` (set `API_BASE_URL` to change).
"""
        )
        return

    forecast, model, validation = st.tabs(
        ["Forecast", "Model", "Validation"]
    )
    with forecast:
        forecast_tab(info)
    with model:
        model_tab(info)
    with validation:
        validation_tab()


if __name__ == "__main__":
    main()
