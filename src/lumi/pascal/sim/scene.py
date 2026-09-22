"""Renders the simulated chamber webcam frame from the live `ChamberModel`.

Two effects, both read off the model every frame -- nothing here drives the model,
it only looks:

1. The sample assembly (holder, clip, everything the base image shows) rotates with
   `Substrate` -- the base image is what the chamber looks like at 0 degrees.
2. A dark mask plate slides over it with `Mask1`, in millimetres, with a narrow slit
   cut into it that lets the sample show through. The mask does not rotate with the
   sample -- it is a separate part in front of it.

This exists so `--src sim` gives the fiducial-marker statistics something realistic to
react to: a marker sitting where the slit is supposed to be should see its intensity
dip and recover as `Mask1` sweeps the slit across it, the same way it would on the
real chamber. It is calibrated against the bundled `YSZ.bmp` (640x480, the holder
roughly centred at (293, 225), the clip's arms tilted about -39 degrees) --
`[pascal.simcam.holder]` / `[pascal.simcam.mask]` in settings.toml need retuning for a
different base image or a real measurement of the mask geometry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from lumi.pascal.sim.model import ChamberModel


@dataclass(frozen=True)
class HolderGeometry:
    """Where the sample holder sits in the *unrotated* base image, in pixels."""

    #: Centre of the holder -- the rotation pivot for the "Substrate" angle, and the
    #: point the mask's slit reaches at `MaskGeometry.center_position_mm`.
    center_x: float
    center_y: float
    #: Diameter of the holder's inner (polished) ring. The mask's own coverage is
    #: sized against this, not the outer disc.
    inner_circle_diameter: float
    #: Tilt of the sample block's edge in the unrotated image, degrees. This is the
    #: axis the mask travels along, and the angle its own edges are drawn parallel to
    #: -- see `_rotated_rect`/`cv2.getRotationMatrix2D` for the sign convention.
    edge_angle: float


@dataclass(frozen=True)
class MaskGeometry:
    """Mask1's mm -> pixel calibration, and how dark it renders."""

    #: Mask1 position, mm, at which the slit is centred on the holder.
    center_position_mm: float
    #: Mask1 position, mm, at which the *whole* mask (opaque plate and slit alike) has
    #: slid entirely out of frame. Together with `center_position_mm` this pins down
    #: the pixels-per-mm scale; everything else interpolates from the two.
    hidden_position_mm: float
    #: +1: increasing Mask1 slides the mask from `hidden_position_mm` toward and
    #: through the centre in the +edge_angle direction; -1 the other way.
    direction: float
    #: How dark the mask renders outside the slit, 0 (invisible) .. 1 (opaque).
    opacity: float


class ChamberSceneRenderer:
    """A `SimCameraConfig.frame_transform`: reads `ChamberModel.snapshot()` and
    returns the frame with the sample rotated and the mask drawn over it."""

    def __init__(self, model: ChamberModel, holder: HolderGeometry, mask: MaskGeometry) -> None:
        self.model = model
        self.holder = holder
        self.mask = mask

        # The mask rectangle, in its own (u, v) frame: u runs along the holder's edge
        # (the travel axis), v across it.
        side = holder.inner_circle_diameter
        self._mask_u = side          # "mostly covers the sample holder"
        self._mask_v = side
        self._slit_v = side * 2.0 / 3.0   # 2/3 the mask's length
        self._slit_u = self._slit_v / 10.0  # 1:10 aspect ratio

        rad = math.radians(holder.edge_angle)
        self._u_hat = np.array([math.cos(rad), math.sin(rad)])
        self._center = np.array([holder.center_x, holder.center_y])
        self._px_per_mm: float | None = None  # lazily computed against the frame size

    def render(self, frame: np.ndarray) -> np.ndarray:
        height, width = frame.shape[:2]
        row = self.model.snapshot()
        angle = float(row.get("Substrate", 0.0) or 0.0)
        mask1_mm = float(row.get("Mask1", 0.0) or 0.0)

        rotated = _rotate(frame, angle, (self.holder.center_x, self.holder.center_y))
        return self._draw_mask(rotated, mask1_mm, height, width)

    # --- calibration ---

    def _scale(self, height: int, width: int) -> float:
        if self._px_per_mm is not None:
            return self._px_per_mm
        # Cast a ray from the holder centre in the direction Mask1 retreats toward
        # (opposite `direction`); the distance to where it leaves the frame, plus half
        # the mask's own extent along that axis, is the offset at which the whole mask
        # has just cleared the frame -- which `hidden_position_mm` is defined to be.
        toward_hidden = -self._u_hat * (1.0 if self.mask.direction >= 0 else -1.0)
        edge = _ray_box_exit(self._center, toward_hidden, width, height)
        span_mm = self.mask.center_position_mm - self.mask.hidden_position_mm
        self._px_per_mm = (edge + self._mask_u / 2.0) / span_mm if span_mm else 0.0
        return self._px_per_mm

    # --- drawing ---

    def _draw_mask(self, frame: np.ndarray, mask1_mm: float, height: int, width: int) -> np.ndarray:
        scale = self._scale(height, width)
        offset = self.mask.direction * (mask1_mm - self.mask.center_position_mm) * scale
        center = self._center + self._u_hat * offset

        opaque = _rotated_rect_mask((height, width), center, self._mask_u, self._mask_v,
                                     self.holder.edge_angle)
        if not opaque.any():
            return frame
        slit = _rotated_rect_mask((height, width), center, self._slit_u, self._slit_v,
                                   self.holder.edge_angle)
        darken = opaque & ~slit

        out = frame.astype(np.float32, copy=True)
        out[darken] *= (1.0 - self.mask.opacity)
        return out


def _rotate(frame: np.ndarray, angle_deg: float, center: tuple[float, float]) -> np.ndarray:
    matrix = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    return cv2.warpAffine(
        frame.astype(np.float32), matrix, (frame.shape[1], frame.shape[0]),
        borderMode=cv2.BORDER_REPLICATE,
    )


def _rotated_rect_mask(
    shape: tuple[int, int], center: np.ndarray, extent_u: float, extent_v: float, angle_deg: float,
) -> np.ndarray:
    """Boolean mask of `shape`, True inside a `extent_u` x `extent_v` rectangle
    centred at `center` and rotated `angle_deg` (the u-axis direction)."""
    height, width = shape
    ys, xs = np.mgrid[0:height, 0:width]
    dx, dy = xs - center[0], ys - center[1]
    rad = math.radians(angle_deg)
    cos_t, sin_t = math.cos(rad), math.sin(rad)
    u = dx * cos_t + dy * sin_t
    v = -dx * sin_t + dy * cos_t
    return (np.abs(u) <= extent_u / 2.0) & (np.abs(v) <= extent_v / 2.0)


def _ray_box_exit(origin: np.ndarray, direction: np.ndarray, width: int, height: int) -> float:
    """Distance along `direction` from `origin` (inside [0,width) x [0,height)) to
    where the ray first leaves it."""
    ts = []
    for o, d, bound in ((origin[0], direction[0], width), (origin[1], direction[1], height)):
        if abs(d) < 1e-9:
            continue
        ts += [(0.0 - o) / d, (bound - o) / d]
    positive = [t for t in ts if t > 0]
    return min(positive) if positive else 0.0
