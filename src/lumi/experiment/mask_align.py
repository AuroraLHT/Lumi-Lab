"""Locating the mask's slit from a fiducial marker's intensity trace.

The automatic half of mask-centre calibration: Mask1 is scanned across a window around
the current `center_mask_pos` while the chamber webcam watches the marker tagged
`mask-center` (the sample's centre). Everywhere in the window the plate covers the
marker and it reads the plate's own flat level; only where the slit passes over it
does the sample show through, so the trace is a flat baseline with one bump. The bump
is centred where the slit is centred on the marker, which is `center_mask_pos`.

The bump's *sign* is not assumed: a sample darker than the plate dips instead of
peaking, so the fit looks for the largest deviation from the baseline either way.
Kept free of any I/O so it is testable against a synthetic trace.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


class SlitNotFound(RuntimeError):
    """The trace has no clear slit signature -- too little contrast, or a bump that
    runs off the edge of the swept window, so its centre cannot be trusted."""


@dataclass(frozen=True)
class SlitFit:
    #: Mask1 position, mm, the slit is centred on the marker at.
    center: float
    #: |peak - baseline|, in intensity units.
    contrast: float
    #: The plate's own reading -- the median of the trace, which is mostly plate.
    baseline: float
    #: +1 the slit reads brighter than the plate, -1 darker.
    polarity: int
    #: How many sweep points fell within the bump (at or past half its height).
    n_points: int
    #: Span, mm, from the first to the last of those points -- how wide a window a
    #: finer re-scan needs to take in the whole bump again.
    width: float


def locate_slit(positions, readings, min_contrast: float = 5.0, baseline: float | None = None) -> SlitFit:
    """Fit the slit centre from a sweep: `positions` (mm, ascending) and the marker
    reading at each.

    `baseline` is the plate's own reading. Left out, it is the median of the trace,
    which is right for a wide scan that is mostly plate; a narrow re-scan that sits
    mostly on the bump must pass the one its wide scan found.

    The bump is the contiguous run of points around the largest deviation from the
    median that stay at or beyond half that deviation; its centre is the
    deviation-weighted mean position of that run, which lands between sweep points
    rather than snapping to one. Raises `SlitNotFound` if the deviation is under
    `min_contrast` or the run reaches either end of the window.
    """
    pos = np.asarray(positions, dtype=float)
    val = np.asarray(readings, dtype=float)
    if pos.shape != val.shape or pos.size < 3:
        raise ValueError("need at least 3 (position, reading) pairs of equal length")

    baseline = float(np.median(val)) if baseline is None else float(baseline)
    dev = val - baseline
    peak = int(np.argmax(np.abs(dev)))
    contrast = float(abs(dev[peak]))
    if contrast < min_contrast:
        raise SlitNotFound(
            f"no slit seen: the marker's reading varied by at most {contrast:.1f} over "
            f"{pos[0]:.2f}..{pos[-1]:.2f} mm (need {min_contrast:.1f}) -- widen the window, "
            "or check the marker tagged for the mask centre is on the sample"
        )
    polarity = 1 if dev[peak] > 0 else -1
    signed = dev * polarity

    lo = hi = peak
    while lo > 0 and signed[lo - 1] >= contrast / 2:
        lo -= 1
    while hi < pos.size - 1 and signed[hi + 1] >= contrast / 2:
        hi += 1
    if lo == 0 or hi == pos.size - 1:
        raise SlitNotFound(
            f"the slit signature runs off the edge of the swept window "
            f"({pos[0]:.2f}..{pos[-1]:.2f} mm) -- re-centre or widen the sweep"
        )

    weights = signed[lo:hi + 1]
    center = float(np.sum(pos[lo:hi + 1] * weights) / np.sum(weights))
    return SlitFit(center=center, contrast=contrast, baseline=baseline, polarity=polarity,
                   n_points=hi - lo + 1, width=float(pos[hi] - pos[lo]))
