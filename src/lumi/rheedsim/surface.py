"""Cutting a crystal along (hkl) and turning it so the beam runs along [uvw].

The surface is described by a primitive cell of the crystal's lattice: two shortest
in-plane lattice vectors a1, a2 (the surface mesh, whose reciprocal lattice is the
rods) and a stacking vector a3 that climbs the least a lattice vector can. The
lattice includes any centring (see Crystal.translations), so Si(111) gets its 3.84 A
mesh, not the conventional cell's 7.68 A one -- which is what "7x7" is counted in.
Stacking copies of that cell down a3 builds the slab.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from fractions import Fraction
from functools import reduce

import numpy as np

from .crystal import Crystal, formula

#: Atoms closer in height than this are one plane.
PLANE_TOL = 0.1


@dataclass(frozen=True)
class Plane:
    height: float
    composition: str
    #: Rows of SurfaceCell.frac.
    atoms: tuple[int, ...]


@dataclass(frozen=True)
class SurfaceCell:
    crystal: Crystal
    normal: tuple[int, int, int]
    #: Rows: a1, a2, a3 as [uvw] of the crystal's cell (fractional when centred).
    T: np.ndarray
    #: Crystal-frame Cartesian columns of a1, a2, a3.
    S: np.ndarray
    #: Outward unit normal, crystal frame.
    n: np.ndarray
    #: Atoms' fractional coordinates in (a1, a2, a3), third one in (-tol, 1 - tol].
    frac: np.ndarray
    #: Each row's index into crystal.sites.
    sites: np.ndarray
    #: Height of one a3 repeat.
    spacing: float
    #: Top-first.
    planes: tuple[Plane, ...]

    @property
    def a1_length(self) -> float:
        return float(np.linalg.norm(self.S[:, 0]))

    @property
    def a2_length(self) -> float:
        return float(np.linalg.norm(self.S[:, 1]))

    @property
    def gamma_deg(self) -> float:
        a, b = self.S[:, 0], self.S[:, 1]
        return math.degrees(math.acos(np.clip(a @ b / np.linalg.norm(a) / np.linalg.norm(b), -1, 1)))

    def mesh_reciprocal(self) -> np.ndarray:
        """Crystal-frame columns b1, b2 with b_i . a_j = 2 pi delta_ij, both in-plane."""
        a1, a2 = self.S[:, 0], self.S[:, 1]
        area = np.cross(a1, a2) @ self.n
        return 2 * math.pi * np.stack([np.cross(a2, self.n), np.cross(self.n, a1)], axis=1) / area


def reduce_int(v) -> tuple[int, ...]:
    g = reduce(math.gcd, (abs(int(x)) for x in v))
    return tuple(int(x) // g for x in v) if g else tuple(int(x) for x in v)


def cut(crystal: Crystal, normal) -> SurfaceCell:
    hkl = reduce_int(normal)
    name = f"({' '.join(map(str, hkl))})"
    A = crystal.orth
    G = crystal.reciprocal @ np.array(hkl, dtype=float)
    n = G / np.linalg.norm(G)

    # Every lattice vector in a box: integer steps of the cell plus each centring.
    r = min(max(4, max(abs(x) for x in hkl) + 2), 24)
    ints = np.array(list(itertools.product(range(-r, r + 1), repeat=3)), dtype=float)
    vecs = np.concatenate([ints + t for t in crystal.translations])
    vecs = vecs[np.linalg.norm(vecs, axis=1) > 1e-9]
    dots = vecs @ np.array(hkl, dtype=float)
    lengths = np.linalg.norm(vecs @ A.T, axis=1)

    flat = np.abs(dots) < 1e-6
    inplane, inlen = vecs[flat], lengths[flat]
    if len(inplane) < 2:
        raise ValueError(f"{name} is too high-index to cut")
    # Ties go to the axis-like, positive direction: [100] before [110] before [-100].
    order = sorted(range(len(inplane)), key=lambda i: (
        round(inlen[i], 5), int(np.count_nonzero(np.abs(inplane[i]) > 1e-9)), tuple(-inplane[i])))
    a1 = inplane[order[0]]

    rising = dots > 1e-6
    if not rising.any():
        raise ValueError(f"no stacking vector found for {name}")
    step = float(dots[rising].min())
    climb = rising & (np.abs(dots - step) < 1e-6)
    a3 = vecs[climb][np.argmin(lengths[climb])]
    spacing = float(a3 @ A.T @ n)

    volume = abs(np.linalg.det(A)) / len(crystal.translations)
    min_area = volume / spacing
    best = None
    for i in order[1:]:
        c = inplane[i]
        cross = np.cross(A @ a1, A @ c)
        # Right-handed with the outward normal; its negative is also a candidate.
        if cross @ n <= 0 or abs(float(np.linalg.norm(cross)) - min_area) > 1e-6 * min_area:
            continue
        cosg = float((A @ a1) @ (A @ c)) / inlen[order[0]] / inlen[i]
        # Shortest; then an obtuse (or right) angle, the usual choice for a hexagonal mesh.
        key = (round(inlen[i], 5), cosg > 1e-9, int(np.count_nonzero(np.abs(c) > 1e-9)), tuple(-c))
        if best is None or key < best[0]:
            best = (key, c)
    if best is None:
        raise ValueError(f"no primitive surface mesh found for {name}")
    a2 = best[1]

    T = np.stack([a1, a2, a3])
    S = A @ T.T
    assert abs(abs(np.linalg.det(S)) - volume) < 1e-6 * volume, (T, volume)

    # The conventional cell's atoms folded into the primitive one: a centred cell
    # holds each of them several times over, once per centring.
    frac = np.mod(np.linalg.solve(S, A @ crystal.frac.T).T, 1.0)
    frac[np.isclose(frac, 1.0, atol=1e-6)] = 0.0
    keep: list[int] = []
    for i in range(len(frac)):
        site = crystal.sites[i]
        if not any(crystal.sites[j].element == site.element
                   and crystal.sites[j].occupancy == site.occupancy
                   and _same_place(S, frac[i], frac[j]) for j in keep):
            keep.append(i)
    frac, sites = frac[keep], np.array(keep)
    # An atom a hair under the next repeat belongs to the plane at 0.
    frac[frac[:, 2] > 1 - PLANE_TOL / spacing, 2] -= 1.0
    heights = frac[:, 2] * spacing

    planes: list[Plane] = []
    for i in np.argsort(-heights, kind="stable"):
        if planes and abs(planes[-1].height - heights[i]) < PLANE_TOL:
            planes[-1] = Plane(planes[-1].height, "", (*planes[-1].atoms, int(i)))
        else:
            planes.append(Plane(float(heights[i]), "", (int(i),)))
    planes = [Plane(p.height, formula([crystal.sites[sites[i]] for i in p.atoms], reduced=False) or "?",
                    p.atoms) for p in planes]
    return SurfaceCell(crystal=crystal, normal=hkl, T=T, S=S, n=n, frac=frac, sites=sites,
                       spacing=spacing, planes=tuple(planes))


def _same_place(S: np.ndarray, f: np.ndarray, g: np.ndarray) -> bool:
    d = f - g
    d -= np.rint(d)
    return float(np.linalg.norm(S @ d)) < 0.01


def pick_termination(cell: SurfaceCell, which: int | str | None) -> int:
    """An index into cell.planes."""
    if which is None:
        return 0
    if isinstance(which, int):
        if not 0 <= which < len(cell.planes):
            raise ValueError(f"termination {which} out of range; this surface has "
                             f"{len(cell.planes)}: {', '.join(p.composition for p in cell.planes)}")
        return which
    want = which.replace(" ", "").lower()
    for i, p in enumerate(cell.planes):
        if p.composition.lower() == want:
            return i
    raise ValueError(f"no {which!r} plane in this surface; it has: "
                     f"{', '.join(p.composition for p in cell.planes)}")


def orientation(cell: SurfaceCell, azimuth, offset_deg: float) -> np.ndarray:
    """Rows are the lab x, y, z axes in the crystal frame; lab = R @ crystal.

    x is the beam's direction along the surface, z the outward normal. A positive
    offset turns the sample counter-clockwise seen from above, so the beam, relative
    to the crystal, turns the other way.
    """
    uvw = np.array(azimuth, dtype=int)
    along = int(np.array(cell.normal) @ uvw)
    if along != 0:
        raise ValueError(
            f"azimuth [{' '.join(map(str, azimuth))}] does not lie in the "
            f"({' '.join(map(str, cell.normal))}) surface (h*u + k*v + l*w = {along}, must be 0)")
    x = cell.crystal.orth @ uvw.astype(float)
    x /= np.linalg.norm(x)
    y = np.cross(cell.n, x)
    phi = math.radians(offset_deg)
    x, y = math.cos(phi) * x - math.sin(phi) * y, math.sin(phi) * x + math.cos(phi) * y
    return np.stack([x, y, cell.n])


def slab(cell: SurfaceCell, top: int, layers: int) -> tuple[np.ndarray, np.ndarray]:
    """Atoms of a slab `layers` repeats deep whose top plane is planes[top].

    Returns crystal-frame Cartesian positions with the top plane at height 0 (so
    every atom is at or below it) and each atom's index into crystal.sites.
    """
    h_top = cell.planes[top].height
    base = cell.S @ cell.frac.T  # (3, N)
    heights = cell.frac[:, 2] * cell.spacing
    pos, idx = [], []
    for j in range(layers + 1):
        z = heights - h_top - j * cell.spacing
        keep = (z <= PLANE_TOL / 2) & (z > -(layers * cell.spacing) - PLANE_TOL / 2)
        if keep.any():
            p = base[:, keep] - j * cell.S[:, 2:3] - h_top * cell.n[:, None]
            pos.append(p.T)
            idx.append(cell.sites[keep])
    return np.concatenate(pos), np.concatenate(idx)


def fraction_label(values) -> str:
    out = []
    for v in values:
        f = Fraction(float(v)).limit_denominator(64)
        out.append(str(f.numerator) if f.denominator == 1 else f"{f.numerator}/{f.denominator}")
    return " ".join(out)
