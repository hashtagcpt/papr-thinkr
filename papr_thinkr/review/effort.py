"""Effort estimates: the effort scale, normalising a model's estimate, adding
estimates up, and describing hour ranges for humans."""
from __future__ import annotations

import math


EFFORT_RANGES = {"trivial": (0.1, 1.0), "small": (1.0, 4.0), "moderate": (4.0, 16.0),
                 "large": (16.0, 80.0), "very_large": (80.0, 400.0)}


def clamp01(x, default: float = 0.5) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if math.isnan(v):
        return default
    if v > 1.0:
        v = v / 100.0 if v <= 100 else 1.0
    return max(0.0, min(1.0, v))


def as_hours(x) -> float | None:
    try:
        v = float(x)
        return v if math.isfinite(v) and v >= 0 else None
    except (TypeError, ValueError):
        return None


def level_for_hours(hours: float) -> str:
    for level, (lo, hi) in EFFORT_RANGES.items():
        if hours <= hi:
            return level
    return "very_large"


def normalize_effort(d: dict) -> dict:
    level = d.get("effort_level") if d.get("effort_level") in EFFORT_RANGES else None
    lo, hi = as_hours(d.get("hours_low")), as_hours(d.get("hours_high"))
    if lo is None and hi is None:
        lo, hi = EFFORT_RANGES[level or "small"]
    elif lo is None:
        lo = hi / 2
    elif hi is None:
        hi = lo * 2
    if lo > hi:
        lo, hi = hi, lo
    hi = max(hi, lo, 0.1)
    if level is None:
        level = level_for_hours(math.sqrt(max(lo, 0.05) * hi))
    return {"level": level, "hours_low": round(lo, 2), "hours_high": round(hi, 2),
            "needs_new_data": bool(d.get("needs_new_data")),
            "confidence": round(clamp01(d.get("effort_confidence"), 0.5), 2)}


def aggregate_effort(efforts: list[dict]) -> dict | None:
    if not efforts:
        return None
    lo = sum(e["hours_low"] for e in efforts)
    hi = sum(e["hours_high"] for e in efforts)
    likely = sum(math.sqrt(max(e["hours_low"], 0.05) * e["hours_high"]) for e in efforts)
    weights = [(e["hours_low"] + e["hours_high"]) / 2 for e in efforts]
    conf = sum(w * e["confidence"] for w, e in zip(weights, efforts)) / (sum(weights) or 1)
    if likely <= 8:
        level = "light"
    elif likely <= 40:
        level = "moderate"
    elif likely <= 160:
        level = "substantial"
    else:
        level = "extensive"
    return {"hours_low": round(lo, 1), "hours_high": round(hi, 1), "hours_likely": round(likely, 1),
            "level": level, "confidence": round(conf, 2), "n_items": len(efforts),
            "needs_new_data": sum(1 for e in efforts if e.get("needs_new_data"))}


def describe_hours(lo: float, hi: float) -> str:
    """'1.5-4 h', '2-8 working days', '30 min - 2 h'."""
    def unit(h):
        if h < 1:
            return "min", 60.0
        if h < 16:
            return "h", 1.0
        if h < 160:
            return "working days", 1 / 8
        return "working weeks", 1 / 40

    def fmt(x):
        return f"{x:.1f}".rstrip("0").rstrip(".") if x < 10 else f"{x:.0f}"
    (ul, fl), (uh, fh) = unit(lo), unit(hi)
    if ul == uh:
        return f"{fmt(lo * fl)}–{fmt(hi * fh)} {uh}"
    return f"{fmt(lo * fl)} {ul} – {fmt(hi * fh)} {uh}"
