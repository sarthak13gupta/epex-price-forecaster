"""
Chart palette and mark specs, resolved for the viewer's theme.

The palette is deliberately NOT the teal/amber used in the project documents.
That teal measures OKLCH chroma 0.075, below the 0.1 floor a data mark needs —
it reads as grey when it carries meaning rather than decoration. It stays as UI
chrome; series colours come from a validated categorical order instead.

Validated with the data-viz palette checker, all-pairs, both modes:
  light  worst CVD dE 9.2, worst normal-vision dE 24.0
  dark   worst CVD dE 9.4, worst normal-vision dE 20.9
One WARN: aqua is 2.74:1 on the light surface, below 3:1. The relief rule
applies, so every chart using it ships visible direct labels AND a table view.
"""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st


@dataclass(frozen=True)
class Palette:
    """Resolved colour tokens for one theme."""

    surface: str
    ink: str
    ink_muted: str
    grid: str
    # Categorical slots 1-3, in fixed order. Only the first three of the
    # reference order clear the all-pairs floors, which is exactly the number
    # of models on the leaderboard.
    series: tuple[str, str, str]

    @property
    def accent(self) -> str:
        """Single-series colour. Slot 1 — a lone series needs no CVD pairing."""
        return self.series[0]


LIGHT = Palette(
    surface="#fcfcfb",
    ink="#0b0b0b",
    ink_muted="#52514e",
    grid="#e6e6e3",
    series=("#2a78d6", "#eb6834", "#1baf7a"),
)

DARK = Palette(
    surface="#1a1a19",
    ink="#ffffff",
    ink_muted="#c3c2b7",
    grid="#2e2e2c",
    # The same three hues re-stepped for the dark surface, not an auto-flip.
    series=("#3987e5", "#d95926", "#199e70"),
)

# Fixed hue per model, keyed by NAME not by rank. A filter that removes one
# model must not repaint the others, so this mapping never depends on ordering.
MODEL_SLOT: dict[str, int] = {
    "XGBoost": 0,
    "ElasticNet": 1,
    "Baseline_Seasonal": 2,
}

# Mark specifications from the data-viz reference.
LINE_WIDTH = 2
MARKER_SIZE = 8
MARKER_RING = 2
BAR_CORNER_RADIUS = 4
BAR_MAX_THICKNESS = 24
GRID_WIDTH = 1


def palette() -> Palette:
    """
    Returns the palette matching the viewer's Streamlit theme.

    Falls back to light when the option is unreadable, which is the safer
    default: light-mode colours on a light surface, rather than dark steps that
    would be low-contrast if the guess were wrong.
    """
    try:
        base = str(st.get_option("theme.base") or "light").lower()
    except Exception:
        base = "light"
    return DARK if base == "dark" else LIGHT


def model_color(model_name: str, pal: Palette) -> str:
    """Stable colour for a model. Unknown names fall back to muted ink."""
    slot = MODEL_SLOT.get(model_name)
    return pal.series[slot] if slot is not None else pal.ink_muted
