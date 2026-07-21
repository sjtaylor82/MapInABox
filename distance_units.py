"""Shared user-facing distance formatting.

Calculations and provider parameters remain metric.  This module only controls
how distances are presented to the user.
"""

from __future__ import annotations

import re


_unit_system = "metric"


def set_unit_system(value: str) -> None:
    global _unit_system
    _unit_system = "imperial" if str(value).lower() == "imperial" else "metric"


def get_unit_system() -> str:
    return _unit_system


def format_distance(metres: float, *, short: bool = False) -> str:
    metres = max(0.0, float(metres or 0.0))
    if _unit_system == "imperial":
        miles = metres / 1609.344
        if miles < 0.1:
            raw_feet = metres * 3.280839895
            feet = (int(round(raw_feet / 10.0) * 10)
                    if raw_feet >= 100 else int(round(raw_feet)))
            return f"{feet} ft" if short else f"{feet} {'foot' if feet == 1 else 'feet'}"
        if miles < 10:
            text = f"{miles:.1f}"
        else:
            text = str(int(round(miles)))
        return f"{text} mi" if short else f"{text} {'mile' if float(text) == 1 else 'miles'}"

    if metres < 1000:
        value = int(round(metres))
        return f"{value} m" if short else f"{value} {'metre' if value == 1 else 'metres'}"
    km = metres / 1000.0
    text = f"{km:.1f}" if km < 10 else str(int(round(km)))
    return f"{text} km" if short else f"{text} {'kilometre' if float(text) == 1 else 'kilometres'}"


def format_height(metres: float, *, short: bool = False) -> str:
    metres = float(metres or 0.0)
    if _unit_system == "imperial":
        feet = int(round(metres * 3.280839895))
        return f"{feet:,} ft" if short else f"{feet:,} {'foot' if feet == 1 else 'feet'}"
    value = int(round(metres))
    return f"{value:,} m" if short else f"{value:,} {'metre' if value == 1 else 'metres'}"


_EMBEDDED_DISTANCE_RE = re.compile(
    r",?\s*\d+(?:\.\d+)?\s*"
    r"(?:m|metres?|meters?|km|kilometres?|kilometers?|ft|feet|foot|mi|miles?)"
    r"\s+[A-Za-z-]+(?=,|\s{2}|$)",
    re.IGNORECASE,
)


def format_distance_label(label: str, metres: float, bearing: str = "") -> str:
    """Replace an embedded distance/bearing segment without refetching data.

    This also migrates labels read from older caches, which stored metric text
    directly in the label rather than retaining it as presentation-only data.
    """
    label = str(label or "").strip()
    suffix = ""
    marker = "  Explorable."
    if marker in label:
        label, suffix = label.split(marker, 1)
        suffix = marker + suffix
    base = _EMBEDDED_DISTANCE_RE.sub("", label, count=1).rstrip(" ,")
    direction = f" {bearing.strip()}" if bearing and bearing.strip() else ""
    return f"{base}, {format_distance(metres)}{direction}{suffix}"
