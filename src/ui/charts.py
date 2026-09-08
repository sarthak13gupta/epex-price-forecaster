"""
Chart builders. Every mark specification lives here so no page can drift.

Two rules shape these charts more than anything else:

1. **No dual axis, ever.** The forecast's drivers are degrees Celsius and
   megawatts. Putting them on two y-scales in one frame invents visual
   correlations that are artefacts of the scaling choice. They are drawn as
   SMALL MULTIPLES instead: separate panels sharing one x-axis.

2. **Colour follows the entity, not its rank.** A model's hue is keyed by name
   in `theme.MODEL_SLOT`, so removing a series never repaints the survivors.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.ui.theme import (
    BAR_CORNER_RADIUS,
    GRID_WIDTH,
    LINE_WIDTH,
    MARKER_RING,
    MARKER_SIZE,
    Palette,
    model_color,
)


def _style_axes(fig: go.Figure, pal: Palette, rows: int = 1) -> None:
    """
    Recessive chrome: hairline solid gridlines one step off the surface, no
    axis lines, no zero line. The data is the only loud thing.
    """
    fig.update_xaxes(
        showgrid=False, showline=False, zeroline=False,
        tickfont=dict(color=pal.ink_muted, size=11),
    )
    fig.update_yaxes(
        showgrid=True, gridcolor=pal.grid, gridwidth=GRID_WIDTH, griddash="solid",
        showline=False, zeroline=False,
        tickfont=dict(color=pal.ink_muted, size=11),
        title_font=dict(color=pal.ink_muted, size=11),
    )
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=pal.ink, size=12),
        margin=dict(l=8, r=8, t=28, b=8),
        hoverlabel=dict(bgcolor=pal.surface, font_size=12,
                        bordercolor=pal.grid, font_color=pal.ink),
    )


def price_path(df: pd.DataFrame, pal: Palette) -> go.Figure:
    """
    The forecast price path. One series, so no legend — the title names it.

    Markers carry a 2px ring in the surface colour so overlapping points at a
    short horizon stay countable.
    """
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["date"], y=df["predicted_price"],
        mode="lines+markers",
        line=dict(color=pal.accent, width=LINE_WIDTH, shape="linear"),
        marker=dict(size=MARKER_SIZE, color=pal.accent,
                    line=dict(width=MARKER_RING, color=pal.surface)),
        name="Forecast",
        hovertemplate="%{x|%a %d %b}<br><b>%{y:.2f}</b> EUR/MWh<extra></extra>",
    ))
    fig.update_layout(
        height=300, showlegend=False, hovermode="x unified",
        yaxis_title="EUR/MWh",
    )
    _style_axes(fig, pal)
    return fig


# Driver panels, in cascade order: the sequence the model actually computes.
DRIVERS: list[tuple[str, str, str]] = [
    ("t_lisse",        "Smoothed national temperature", "°C"),
    ("demand_mw",      "Simulated demand",              "MW"),
    ("residual_demand","Residual demand (demand − nuclear)", "MW"),
]


def driver_panels(df: pd.DataFrame, pal: Palette) -> go.Figure:
    """
    The exogenous state the cascade assumed, as small multiples.

    Deliberately not a dual-axis overlay. Each panel has its own y-scale and
    they share the x-axis, so a reader can trace a date down the stack without
    being invited to compare degrees against megawatts.

    Drawn subordinate to the price panel — thinner marks, shorter panels — because
    these explain the answer rather than being it.
    """
    fig = make_subplots(
        rows=len(DRIVERS), cols=1, shared_xaxes=True, vertical_spacing=0.09,
        subplot_titles=[f"{label}  ({unit})" for _, label, unit in DRIVERS],
    )

    for row, (col, label, unit) in enumerate(DRIVERS, start=1):
        fig.add_trace(
            go.Scatter(
                x=df["date"], y=df[col],
                mode="lines",
                line=dict(color=pal.accent, width=LINE_WIDTH, shape="linear"),
                name=label,
                hovertemplate=f"%{{x|%a %d %b}}<br><b>%{{y:,.1f}}</b> {unit}<extra></extra>",
            ),
            row=row, col=1,
        )

    fig.update_layout(
        height=110 * len(DRIVERS) + 60, showlegend=False, hovermode="x unified",
    )
    _style_axes(fig, pal)
    for annotation in fig.layout.annotations:
        annotation.font.update(size=11, color=pal.ink_muted)
    return fig


def leaderboard_bars(rows: list[dict], pal: Palette, metric: str = "mae") -> go.Figure:
    """
    Mean fold error per model. Horizontal bars: magnitude across a few named
    categories, and horizontal keeps the model names readable unrotated.

    Values are labelled directly on every bar. That is required, not optional —
    the aqua slot sits below 3:1 on the light surface, so the palette validator
    obliges visible labels or a table view. This ships both.
    """
    frame = pd.DataFrame(rows).sort_values(metric, ascending=False)
    label = {"mae": "Mean MAE", "rmse": "Mean RMSE"}.get(metric, metric.upper())

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=frame["model"], x=frame[metric], orientation="h",
        marker=dict(
            color=[model_color(m, pal) for m in frame["model"]],
            cornerradius=BAR_CORNER_RADIUS, line_width=0,
        ),
        width=0.62,   # cap the bar; the band's leftover is deliberate air
        text=[f"{v:.2f}" for v in frame[metric]],
        textposition="outside",
        textfont=dict(color=pal.ink, size=12),   # ink, never the series colour
        hovertemplate="<b>%{y}</b><br>" + label + ": %{x:.3f} EUR/MWh<extra></extra>",
    ))
    fig.update_layout(
        height=52 * len(frame) + 90, showlegend=False,
        xaxis_title=f"{label} (EUR/MWh) — lower is better",
        bargap=0.35,
    )
    _style_axes(fig, pal)
    # Magnitude bars need the value axis gridded, the category axis bare.
    fig.update_xaxes(showgrid=True, gridcolor=pal.grid, gridwidth=GRID_WIDTH,
                     range=[0, float(frame[metric].max()) * 1.18])
    fig.update_yaxes(showgrid=False, tickfont=dict(color=pal.ink, size=12))
    return fig


def regime_bars(rows: list[dict], pal: Palette) -> go.Figure:
    """
    Per-regime MAE, grouped by model.

    This is the chart that carries the analytical point: a single mean hides
    where a model fails. Grouped horizontal bars keep the long regime labels
    legible and let the three models be compared within each regime.

    Three series, so a legend is always present — identity is never colour
    alone.
    """
    frame = pd.DataFrame(rows)
    regime_col = frame.columns[0]
    models = [c for c in frame.columns if c != regime_col]

    fig = go.Figure()
    for model in models:
        fig.add_trace(go.Bar(
            y=frame[regime_col], x=frame[model], orientation="h", name=model,
            marker=dict(color=model_color(model, pal),
                        cornerradius=BAR_CORNER_RADIUS, line_width=0),
            hovertemplate="<b>%{y}</b><br>" + model + ": %{x:.2f} EUR/MWh<extra></extra>",
        ))

    fig.update_layout(
        height=46 * len(frame) + 150,
        barmode="group",
        bargap=0.3,
        bargroupgap=0.08,      # the 2px-equivalent surface gap between marks
        xaxis_title="MAE (EUR/MWh) — lower is better",
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="left", x=0, font=dict(color=pal.ink, size=11),
                    title=None),
    )
    _style_axes(fig, pal)
    fig.update_xaxes(showgrid=True, gridcolor=pal.grid, gridwidth=GRID_WIDTH)
    fig.update_yaxes(showgrid=False, autorange="reversed",
                     tickfont=dict(color=pal.ink, size=11))
    return fig
