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
    """Mask1's mm -> pixel calibration, and how the plate renders."""

    #: Mask1 position, mm, at which the slit is centred on the holder.
    center_position_mm: float
    #: Mask1 position, mm, at which the *whole* mask (opaque plate and slit alike) has
    #: slid entirely out of frame. Together with `center_position_mm` this pins down
    #: the pixels-per-mm scale; everything else interpolates from the two.
    hidden_position_mm: float
    #: +1: increasing Mask1 slides the mask from `hidden_position_mm` toward and
    #: through the centre in the +edge_angle direction; -1 the other way.
    direction: float
    #: How far the plate extends beyond its holder-sized head, in pixels, on the side
    #: away from the slit -- the arm a real mask hangs off of, mounted outside the
    #: frame rather than floating free over the sample. Long enough by default to run
    #: off the edge of a 640x480 frame; the *slit*'s own position and size are
    #: unaffected, they stay sized off `HolderGeometry.inner_circle_diameter` as before.
    arm_length: float = 500.0
    #: Grey level (0..255) the plate renders outside the slit.
    color: float = 100.0
    #: How much of `color` replaces the frame under the plate, 0 (invisible) .. 1 (a
    #: flat, fully opaque colour -- no texture of what is underneath shows through).
    alpha: float = 1.0


class ChamberSceneRenderer:
    """A `SimCameraConfig.frame_transform`: reads `ChamberModel.snapshot()` and
    returns the frame with the sample rotated and the mask drawn over it."""

    def __init__(self, model: ChamberModel, holder: HolderGeometry, mask: MaskGeometry) -> None:
        self.model = model
        self.holder = holder
        self.mask = mask

        # The mask rectangle, in its own (u, v) frame: u runs along the holder's edge
        # (the travel axis) -- the plate's *short* edges are the ones at u = +-side/2,
        # parallel to it. v runs the plate's own length: across the head at
        # v in [-side/2, side/2], then on into the arm, `arm_length` further in -v,
        # off toward where the real thing would be mounted, roughly top-left to
        # bottom-right, outside the frame.
        side = holder.inner_circle_diameter
        self._mask_u = side          # "mostly covers the sample holder"
        self._mask_v_bounds = (-(side / 2.0 + mask.arm_length), side / 2.0)
        # The slit itself is cut across the head, parallel to the plate's *short*
        # edges: its long axis (2/3 the head's own side) runs along u, its narrow axis
        # (1:10 of that) along v -- so it is a letterbox slot the width of the plate,
        # not a sliver running off down the arm.
        self._slit_u = side * 2.0 / 3.0   # 2/3 the head's length, parallel to the short edge
        self._slit_v = self._slit_u / 10.0  # 1:10 aspect ratio

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
        half_u = self._mask_u / 2.0

        opaque = _rotated_rect_mask((height, width), center, (-half_u, half_u),
                                     self._mask_v_bounds, self.holder.edge_angle)
        if not opaque.any():
            return frame
        half_slit_u, half_slit_v = self._slit_u / 2.0, self._slit_v / 2.0
        slit = _rotated_rect_mask((height, width), center, (-half_slit_u, half_slit_u),
                                   (-half_slit_v, half_slit_v), self.holder.edge_angle)
        painted = opaque & ~slit

        out = frame.astype(np.float32, copy=True)
        out[painted] = out[painted] * (1.0 - self.mask.alpha) + self.mask.color * self.mask.alpha
        return out


def _rotate(frame: np.ndarray, angle_deg: float, center: tuple[float, float]) -> np.ndarray:
    matrix = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    return cv2.warpAffine(
        frame.astype(np.float32), matrix, (frame.shape[1], frame.shape[0]),
        borderMode=cv2.BORDER_REPLICATE,
    )


def _rotated_rect_mask(
    shape: tuple[int, int],
    center: np.ndarray,
    u_bounds: tuple[float, float],
    v_bounds: tuple[float, float],
    angle_deg: float,
) -> np.ndarray:
    """Boolean mask of `shape`, True inside the rectangle `u_bounds` x `v_bounds` (in
    the frame centred on `center` and rotated `angle_deg`, the u-axis direction) --
    not necessarily centred on `center` itself, since a bound pair need not be
    symmetric (the mask's arm extends further one way than the other)."""
    height, width = shape
    ys, xs = np.mgrid[0:height, 0:width]
    dx, dy = xs - center[0], ys - center[1]
    rad = math.radians(angle_deg)
    cos_t, sin_t = math.cos(rad), math.sin(rad)
    u = dx * cos_t + dy * sin_t
    v = -dx * sin_t + dy * cos_t
    return (u_bounds[0] <= u) & (u <= u_bounds[1]) & (v_bounds[0] <= v) & (v <= v_bounds[1])


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
