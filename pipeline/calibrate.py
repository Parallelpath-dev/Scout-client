"""
Scout — calibrate a weekly number against what is normal for this client.

Standard library only, and it imports nothing from this repo, on purpose: the internal
tool can copy this file as it is and calibrate its own pressure score without taking a
dependency on the client build.

THE IDEA
--------
A raw score says how much pressure there is. A calibrated score says how unusual this
week is FOR THESE COMPETITORS. If a set of competitors scores 70 every week, 70 is
their normal and it reads 50. If they usually move within ten points, ten points is
normal movement and only a move beyond it reads as a ramp.

    50        a normal week for this set
    65 / 35   about one usual swing above / below
    80 / 20   about two
    95+ / 5-  far outside anything in the lookback

HOW
---
Median and MAD over the lookback, not mean and standard deviation. One freak week
should not redefine normal for the next quarter, and the median ignores it.

The FLOOR is the part that matters most. A history that has not moved at all has a
spread of zero, and dividing by zero turns the first one-point wobble into a crisis.
The floor sets the smallest move worth calling a move:

    abs_floor   in the raw units. The internal tool's model scores move in steps of 10,
                so 10 there: one step is worth about 15 calibrated points, not 30.
    rel_floor   a fraction of the median. For counts: a competitor running 300 ads has
                to add more than one to register, one running 3 does not.

Backtested on internal history, 24 Sep 2026: Mattress Warehouse's June move from 42 to
72 reads 88 to 93, and once 72 had held for a quarter it read 50 again.
"""

from __future__ import annotations

from statistics import median
from typing import Iterable, Sequence

MAD_TO_SD = 1.4826   # MAD x this estimates a standard deviation for normal data
LOOKBACK = 12        # weeks
MIN_HISTORY = 4      # weeks before a number is calibrated at all
K = 15               # calibrated points per usual swing
Z_CAP = 3.0


def robust_z(
    value: float | None,
    history: Iterable[float | None],
    *,
    abs_floor: float = 1.0,
    rel_floor: float = 0.0,
    min_history: int = MIN_HISTORY,
    lookback: int = LOOKBACK,
    cap: float = Z_CAP,
) -> float | None:
    """How many usual swings `value` sits from this series' normal.

    history is oldest first and must NOT include this week. None when there is not
    enough history to say what normal is; the caller shows "calibrating", never a guess.
    """
    if value is None:
        return None
    h = [float(x) for x in history if x is not None][-lookback:]
    if len(h) < min_history:
        return None
    med = median(h)
    spread = max(MAD_TO_SD * median(abs(x - med) for x in h), abs_floor, rel_floor * abs(med))
    z = (float(value) - med) / spread
    return max(-cap, min(cap, z))


def to_score(z: float | None, bonus: float = 0.0, k: float = K) -> int | None:
    """A z on the 0-100 scale, with any fixed-point bonus added after calibration."""
    if z is None:
        return None
    return int(max(0, min(100, round(50 + k * z + bonus))))


def calibrate_score(
    value: float | None,
    history: Sequence[float | None],
    *,
    abs_floor: float = 10.0,
    lookback: int = LOOKBACK,
    min_history: int = MIN_HISTORY,
) -> int | None:
    """Drop-in for a tool that already has a 0-100 score, like the internal pressure
    score. Returns None while calibrating."""
    return to_score(robust_z(value, history, abs_floor=abs_floor,
                             lookback=lookback, min_history=min_history))


def trend(history_and_now: Sequence[float | None], *, window: int = 4,
          min_len: int = 12, threshold: float = 0.15) -> str | None:
    """The slow reading. Calibration absorbs a sustained ramp into the new normal, which
    is right for "is this week unusual" and wrong for "is the market hotter than last
    quarter". This compares the latest `window` weeks with the earliest `window` weeks
    of the lookback, so a ramp that lasted still shows."""
    h = [float(x) for x in history_and_now if x is not None][-LOOKBACK:]
    if len(h) < min_len:
        return None
    then, now = median(h[:window]), median(h[-window:])
    base = max(abs(then), 1.0)
    change = (now - then) / base
    if change > threshold:
        return "hotter"
    if change < -threshold:
        return "cooler"
    return "steady"
