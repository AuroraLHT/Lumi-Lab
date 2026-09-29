"""The beam and the screen: where on the image an outgoing wave lands, and back.

Lab frame (see lumi.contracts.payloads.simulation): z is the surface normal, x the
beam's direction along the surface, the screen square to x at the camera length.
Seen looking down the beam, lab +y is to the left; an unflipped image has row 0 at
the top, so screen +z (up) is towards row 0 and +y towards column 0. `roll_deg`
then turns the picture and the flips mirror it, in that order.

A beam shifted off the camera axis (BeamSpec.shift_y_mm, shift_z_mm) starts every
ray from that point instead, so a wave lands `shift` further along the screen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from lumi.contracts.payloads.simulation import ScreenSpec

# CODATA 2018.
_H = 6.62607015e-34
_M0 = 9.1093837015e-31
_E = 1.602176634e-19
_C = 299792458.0


def wavelength(energy_kev: float) -> float:
    """Relativistic electron wavelength, angstrom. 20 keV -> 0.0859 A."""
    ev = energy_kev * 1e3
    p = math.sqrt(2 * _M0 * _E * ev * (1 + _E * ev / (2 * _M0 * _C ** 2)))
    return _H / p * 1e10


@dataclass(frozen=True)
class Screen:
    spec: ScreenSpec
    origin: tuple[float, float]
    #: Where the beam meets the sample, (y, z) mm off the camera axis.
    shift: tuple[float, float] = (0.0, 0.0)

    @classmethod
    def resolve(cls, spec: ScreenSpec) -> Screen:
        """Fills in a missing origin: mid-width, and 10 % of the height in from the
        edge the horizon faces (the pattern rises from it into the image)."""
        partial = cls(spec, (spec.width_px / 2, spec.height_px / 2))
        if spec.origin_x_px is not None and spec.origin_y_px is not None:
            return cls(spec, (spec.origin_x_px, spec.origin_y_px))
        du, dv = partial._to_image_offset(np.array(0.0), np.array(1.0))
        norm = math.hypot(float(du), float(dv))
        ox = spec.width_px / 2 - 0.4 * spec.height_px * float(du) / norm
        oy = spec.height_px / 2 - 0.4 * spec.height_px * float(dv) / norm
        return cls(spec, (spec.origin_x_px if spec.origin_x_px is not None else ox,
                          spec.origin_y_px if spec.origin_y_px is not None else oy))

    def shifted(self, y_mm: float, z_mm: float) -> Screen:
        return replace(self, shift=(float(y_mm), float(z_mm)))

    def filled(self) -> ScreenSpec:
        return self.spec.model_copy(update={"origin_x_px": self.origin[0], "origin_y_px": self.origin[1]})

    # --- screen (mm) <-> image (px) -----------------------------------------

    def _to_image_offset(self, y, z):
        p = self.spec.pixel_size_mm
        du, dv = -np.asarray(y) / p, -np.asarray(z) / p
        rho = math.radians(self.spec.roll_deg)
        du, dv = math.cos(rho) * du + math.sin(rho) * dv, -math.sin(rho) * du + math.cos(rho) * dv
        if self.spec.flip_x:
            du = -du
        if self.spec.flip_y:
            dv = -dv
        return du, dv

    def to_image(self, y, z):
        """Image pixel of a wave whose direction reaches (y, z) on the screen from the
        camera axis; the beam's shift is added here."""
        du, dv = self._to_image_offset(np.asarray(y) + self.shift[0], np.asarray(z) + self.shift[1])
        return du + self.origin[0], dv + self.origin[1]

    def to_screen(self, u, v):
        du, dv = np.asarray(u, float) - self.origin[0], np.asarray(v, float) - self.origin[1]
        if self.spec.flip_x:
            du = -du
        if self.spec.flip_y:
            dv = -dv
        rho = math.radians(self.spec.roll_deg)
        du, dv = math.cos(rho) * du - math.sin(rho) * dv, math.sin(rho) * du + math.cos(rho) * dv
        p = self.spec.pixel_size_mm
        return -du * p - self.shift[0], -dv * p - self.shift[1]

    # --- wavevectors ---------------------------------------------------------

    def k_out(self, u, v, k: float) -> np.ndarray:
        """The outgoing wavevector that lands on each pixel, shape (3, *u.shape)."""
        y, z = self.to_screen(u, v)
        L = self.spec.camera_length_mm
        norm = np.sqrt(L * L + y * y + z * z)
        return np.stack([k * L / norm, k * y / norm, k * z / norm])

    def pixel_of(self, k_out: np.ndarray):
        """Where a wave going along k_out lands; NaN if it never reaches the screen."""
        kx, ky, kz = k_out
        with np.errstate(divide="ignore", invalid="ignore"):
            t = np.where(kx > 0, self.spec.camera_length_mm / kx, np.nan)
        return self.to_image(ky * t, kz * t)

    def inside(self, u, v, margin: float = 0.0):
        return ((u >= -margin) & (u < self.spec.width_px + margin)
                & (v >= -margin) & (v < self.spec.height_px + margin))
