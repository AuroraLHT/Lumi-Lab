"""Everything a backend needs, resolved once: the crystal cut and turned, and the optics.

A backend (kinematic now, dynamical later) takes a Scene and computes intensities;
it never re-derives geometry. That is also what a geometry fit will vary -- incidence,
azimuth offset and the screen -- without touching the crystal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache

import gemmi
import numpy as np

from lumi.contracts.payloads.simulation import (
    RheedSimRequest,
    ScreenSpec,
    StructureSummary,
    SurfaceMesh,
)

from .crystal import Crystal
from .geometry import Screen, wavelength
from .surface import SurfaceCell, cut, orientation, pick_termination, reduce_int

#: Depth (in amplitude attenuation lengths) the slab is summed to: e^-6.9 = 1e-3.
_DEPTH = 6.9


@dataclass
class Scene:
    request: RheedSimRequest
    crystal: Crystal
    cell: SurfaceCell
    #: Index of the top plane in cell.planes.
    top: int
    #: lab = R @ crystal frame.
    R: np.ndarray
    screen: Screen
    lam: float
    k: float
    theta: float
    layers: int
    warnings: list[str] = field(default_factory=list)

    @property
    def k_in(self) -> np.ndarray:
        return np.array([self.k * math.cos(self.theta), 0.0, -self.k * math.sin(self.theta)])

    def mesh_lab(self) -> np.ndarray:
        """In-plane lab (x, y) columns of the rod lattice's b1, b2."""
        return (self.R @ self.cell.mesh_reciprocal())[:2]

    def attenuation(self, kz_out) -> np.ndarray:
        """Amplitude decay per angstrom of depth, for waves leaving at kz_out."""
        mfp = self.request.morphology.mean_free_path_nm * 10
        sin_out = np.maximum(np.asarray(kz_out) / self.k, 1e-3)
        return (1 / math.sin(self.theta) + 1 / sin_out) / (2 * mfp)

    def zone_repeat(self) -> float | None:
        """Length of the lattice row the beam runs along; None off a zone axis.

        The shortest lattice vector along [uvw], which in a centred cell can be a
        fraction of it: fcc [1-10] repeats every half of [1-10].
        """
        if self.request.surface.azimuth_offset_deg % 360:
            return None
        uvw = np.array(reduce_int(self.request.surface.azimuth), float)
        for d in range(12, 0, -1):
            if self.crystal.is_lattice_vector(uvw / d):
                return float(np.linalg.norm(self.crystal.orth @ (uvw / d)))
        return float(np.linalg.norm(self.crystal.orth @ uvw))

    # --- the parts of the result that do not depend on the backend ----------

    def summary(self) -> StructureSummary:
        return summarize(self.crystal)

    def mesh(self) -> SurfaceMesh:
        c = self.cell
        return SurfaceMesh(
            a1=[_clean(x) for x in c.T[0]], a2=[_clean(x) for x in c.T[1]],
            a3=[_clean(x) for x in c.T[2]],
            a1_length=round(c.a1_length, 4), a2_length=round(c.a2_length, 4),
            gamma_deg=round(c.gamma_deg, 3), layer_spacing=round(c.spacing, 4),
            terminations=[p.composition for p in c.planes],
            termination=c.planes[self.top].composition, n_layers=self.layers,
        )

    def landmarks(self) -> dict:
        s = self.screen
        L, t = s.spec.camera_length_mm, math.tan(self.theta)
        span = (s.spec.width_px + s.spec.height_px) * s.spec.pixel_size_mm
        edge = [s.to_image(y, 0.0) for y in (-span, span)]
        return {
            "origin_px": [round(s.origin[0], 2), round(s.origin[1], 2)],
            "shadow_edge_px": [[round(float(u), 2), round(float(v), 2)] for u, v in edge],
            "specular_px": [round(float(x), 2) for x in s.to_image(0.0, L * t)],
            "direct_beam_px": [round(float(x), 2) for x in s.to_image(0.0, -L * t)],
        }


def _clean(x: float) -> float:
    return float(round(x)) if abs(x - round(x)) < 1e-9 else round(float(x), 6)


def summarize(crystal: Crystal) -> StructureSummary:
    return StructureSummary(
        name=crystal.name, formula=crystal.formula,
        source=crystal.source if crystal.source in ("builtin", "saved") else "inline",
        space_group=crystal.space_group, lattice=crystal.lattice,
        n_atoms=len(crystal.sites), description=crystal.description,
    )


_cuts: dict[tuple[str, tuple[int, ...]], SurfaceCell] = {}


def _cut(crystal: Crystal, normal: tuple[int, ...]) -> SurfaceCell:
    """Cutting searches a few thousand lattice vectors; a fit or a slider re-asks it
    the same question hundreds of times."""
    if not crystal.key:
        return cut(crystal, normal)
    key = (crystal.key, normal)
    if key not in _cuts:
        if len(_cuts) >= 64:
            _cuts.pop(next(iter(_cuts)))
        _cuts[key] = cut(crystal, normal)
    return _cuts[key]


def build(req: RheedSimRequest, crystal: Crystal, screen: ScreenSpec) -> Scene:
    cell = _cut(crystal, tuple(req.surface.normal))
    top = pick_termination(cell, req.surface.termination)
    R = orientation(cell, req.surface.azimuth, req.surface.azimuth_offset_deg)
    lam = wavelength(req.beam.energy_kev)
    k = 2 * math.pi / lam
    theta = math.radians(req.beam.incidence_deg)
    warnings: list[str] = []

    layers = req.morphology.layers
    if layers is None:
        mfp = req.morphology.mean_free_path_nm * 10
        mu_min = (1 / math.sin(theta) + 1) / (2 * mfp)
        layers = min(max(math.ceil(_DEPTH / mu_min / cell.spacing), 2), 300)
        if layers == 300:
            warnings.append("the slab was capped at 300 layers; the mean free path reaches deeper")

    return Scene(request=req, crystal=crystal, cell=cell, top=top, R=R,
                 screen=Screen.resolve(screen), lam=lam, k=k, theta=theta,
                 layers=layers, warnings=warnings)


#: form_factor() reads a table this fine rather than evaluating five exponentials per
#: point; s^2 up to 40 is |q| ~ 80 1/A, past anything a screen sees.
_S2_MAX, _S2_N = 40.0, 8192


def form_factor(element: str, s2: np.ndarray) -> np.ndarray:
    """Electron scattering factor (International Tables C 4.3.2.2), angstrom, at
    s^2 = (sin(theta)/lambda)^2 = |q|^2 / (16 pi^2)."""
    return np.interp(s2, _S2_GRID, _table(element))


_S2_GRID = np.linspace(0.0, _S2_MAX, _S2_N)


@lru_cache(maxsize=128)
def _table(element: str) -> np.ndarray:
    c = gemmi.Element(element).c4322
    if c is None:
        raise ValueError(f"no electron scattering factor for {element}")
    coefs = np.array(c.get_coefs(), dtype=float)
    a, b = coefs[:5], coefs[5:]
    return (a[:, None] * np.exp(-b[:, None] * _S2_GRID[None, :])).sum(axis=0)
