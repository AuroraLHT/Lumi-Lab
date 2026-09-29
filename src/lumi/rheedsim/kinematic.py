"""Kinematic RHEED: single scattering from a slab, read off the Ewald sphere.

A flat surface is periodic in-plane only, so its reciprocal space is a lattice of
rods (the surface mesh's reciprocal lattice) running along the normal. Along each
rod the amplitude is the slab's structure factor,

    A(g, qz) = sum over atoms  occ f_e(s) exp(-B s^2) exp(i g.r) exp((i qz + mu) z),

with z <= 0 below the surface and mu the electrons' attenuation into and back out
of the crystal -- which is why only the top few planes count at grazing angles.
Each pixel of the screen is one outgoing wavevector, so one q; a rod lights the
pixels whose in-plane q is near it, weighted by |A|^2 at that pixel's qz. A finite
terrace makes rods wide, and a wide rod cut by a grazing Ewald sphere is a streak.

3D islands scatter in transmission instead: bulk reciprocal-lattice points,
broadened by the island size, wherever the Ewald sphere passes near one.

What this leaves out -- multiple scattering (Kikuchi lines, intensity oscillations
with incidence), refraction at the surface and inelastic background -- is the
dynamical backend's job, which is why intensities here are qualitative. Positions
are not: they are pure geometry.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

import numpy as np

from lumi.contracts.payloads.simulation import RheedSpot

from .scene import Scene, form_factor
from .surface import fraction_label, slab

#: Samples along each rod. A Bragg feature on a rod is ~2 pi / (penetration depth)
#: wide, several 1/A; this resolves it with room to spare.
N_QZ = 768
#: Spot kinds, in the order the list gives them.
KINDS = ("rod", "fractional", "streak_max", "bulk")
#: A Bragg maximum on a rod is labelled by the bulk point it sits at when its indices
#: are this close to whole numbers (the slab's finite depth shifts peaks slightly).
_BRAGG_TOL = 0.15
#: Spots listed per kind, strongest first.
MAX_SPOTS = 2000
#: Gaussian sigma per FWHM.
_FWHM = 2 * math.sqrt(2 * math.log(2))
#: Rods further than this many sigma from a pixel contribute nothing (e^-4.5 = 1 %).
_REACH = 3.0
#: A pixel sees what falls within this many pixels (a Gaussian sigma) of its centre.
#: A square pixel's own spread is 1/sqrt(12) = 0.29, but a Gaussian that narrow,
#: summed over the pixel grid, gives a spot on a pixel's edge half the total of one
#: on its centre; from 0.6 the total no longer depends on where the spot falls
#: (to 0.2 %), so a pair of mirror-image spots come out equally bright.
_PIXEL_SIGMA = 0.6


@dataclass
class _Lattice:
    """A lattice of rods and |A|^2 along each: the bulk-terminated surface, or one
    reconstruction's superlattice (with its integer-order rods zeroed)."""

    kind: str
    #: Lab (x, y) columns of the lattice's basis.
    B: np.ndarray
    #: Coefficients -> 1x1 surface-mesh indices (identity for the surface itself).
    to_mesh: np.ndarray
    m0: int
    n0: int
    #: (n_m, n_n): each rod's row in `table`, -1 for a rod that never nears the screen.
    index: np.ndarray
    #: (n_rods, N_QZ): |A|^2 along each rod that does.
    table: np.ndarray

    def coeffs(self) -> np.ndarray:
        """Lattice coefficients of the rods in `table`, in its row order."""
        i, j = np.nonzero(self.index >= 0)
        order = np.argsort(self.index[i, j])
        return np.stack([i[order] + self.m0, j[order] + self.n0], axis=1).astype(float)

    def spacing(self) -> float:
        return min(float(np.linalg.norm(self.B @ np.array(v)))
                   for v in ((1, 0), (0, 1), (1, 1), (1, -1)))


@dataclass(frozen=True)
class _Spread:
    """How far a rod's intensity reaches off its centre, in q (1/A): Gaussian
    variances along lab x (the beam) and y in-plane, and the width it is smoothed
    over along the rod."""

    var_x: float
    var_y: float
    sigma_qz: float


class Kinematic:
    name = "kinematic"

    def run(self, scene: Scene, *, image: bool) -> tuple[list[RheedSpot], np.ndarray | None]:
        sp = scene.screen.spec
        u, v = np.meshgrid(np.arange(sp.width_px, dtype=float), np.arange(sp.height_px, dtype=float))
        k_out = scene.screen.k_out(u, v, scene.k)
        q = k_out - scene.k_in[:, None, None]
        valid = k_out[2] > 0

        qz0 = scene.k * math.sin(scene.theta)
        qz_top = float(q[2][valid].max()) if valid.any() else qz0 + 1.0
        qz = np.linspace(qz0, qz_top + 0.05 * (qz_top - qz0) + 1e-3, N_QZ)

        spread = self._rod_spread(scene)
        foot = _footprint(q)
        reach = _REACH * math.sqrt(spread.var_x + spread.var_y
                                   + (float((foot[0] + foot[2])[valid].max()) if valid.any() else 0.0))
        lattices = self._lattices(scene, q[:2][:, valid], qz, spread, reach)
        spots = [s for lat in lattices for s in self._rod_spots(scene, lat, qz)]
        spots += self._streak_maxima(scene, lattices[0], qz, spread)
        bulk = self._bulk_points(scene, q, valid) if scene.request.morphology.islands > 0 else []
        spots += self._bulk_spots(scene, bulk)
        _normalise(spots)
        if len(spots) > MAX_SPOTS:
            scene.warnings.append(f"{len(spots)} spots; listing the {MAX_SPOTS} strongest of each kind")
            spots = [s for kind in KINDS
                     for s in [x for x in spots if x.kind == kind][:MAX_SPOTS]]
        if not any(s.in_view for s in spots):
            scene.warnings.append(
                "nothing reaches the screen: check the incidence angle, camera length "
                "and pixel size (the specular spot is at "
                f"{scene.landmarks()['specular_px']})")

        if not image:
            return spots, None
        return spots, self._render(scene, q, valid, qz, lattices, spread, foot, bulk)

    # --- rods ----------------------------------------------------------------

    def _rod_spread(self, scene: Scene) -> _Spread:
        """How far each rod's intensity reaches, in q.

        Terraces widen a rod the same way in every in-plane direction. Divergence does
        not: an incident wave tilted by an angle d moves q = k_out - k_in by |k_in| d
        at right angles to k_in. Tilted sideways (azimuthally) that is along y; tilted
        up or down it is almost straight along the rod, |k_in| d cos(theta), and only
        |k_in| d sin(theta) along x -- 30 times less at 2 degrees. So divergence blurs a
        spot on the screen by its own angle, and does not make a streak.
        """
        m, b = scene.request.morphology, scene.request.beam
        terrace = 2 * math.pi / (m.terrace_nm * 10) / _FWHM
        tilt = scene.k * b.divergence_mrad * 1e-3 / _FWHM
        return _Spread(var_x=terrace ** 2 + (tilt * math.sin(scene.theta)) ** 2,
                       var_y=terrace ** 2 + (tilt * math.cos(scene.theta)) ** 2,
                       sigma_qz=tilt * math.cos(scene.theta))

    def _lattices(self, scene: Scene, q_par: np.ndarray, qz: np.ndarray, spread: _Spread,
                  reach: float) -> list[_Lattice]:
        B = scene.mesh_lab()
        out = [self._surface(scene, B, q_par, qz, spread, reach)]
        for rec in scene.request.surface.reconstructions:
            M = np.array(rec.matrix, dtype=float)
            Minv = np.linalg.inv(M)
            out.append(self._reconstruction(scene, B @ Minv, Minv, rec.strength, q_par, qz, reach))
        return out

    def _span(self, B: np.ndarray, q_par: np.ndarray, reach: float) -> tuple[range, range]:
        if q_par.size == 0:
            return range(0, 1), range(0, 1)
        f = np.linalg.solve(B, q_par)
        pad = [math.ceil(reach / np.linalg.norm(B[:, i])) + 1 for i in (0, 1)]
        # A reciprocal basis can be much shorter along one axis than its rows are
        # apart; cap the table rather than let a huge cell eat the memory.
        lo = np.floor(f.min(axis=1)).astype(int) - pad
        hi = np.ceil(f.max(axis=1)).astype(int) + pad
        if (hi - lo + 1).prod() > 4_000_000:
            raise ValueError("too many rods reach the screen (the surface cell is very large "
                             "or the screen very wide); lower the resolution or the reconstruction")
        return range(lo[0], hi[0] + 1), range(lo[1], hi[1] + 1)

    def _surface(self, scene: Scene, B, q_par, qz, spread, reach) -> _Lattice:
        ms, ns = self._span(B, q_par, reach)
        mn = _grid(ms, ns)
        use = _limit(_reached(B, mn, q_par, reach))
        g = B @ mn[use].T  # (2, n_rods)

        pos, idx = slab(scene.cell, scene.top, scene.layers)
        P = scene.R @ pos.T  # (3, n_atoms), lab frame
        in_plane = np.exp(1j * (g.T @ P[:2]))  # (n_rods, n_atoms)
        mu = scene.attenuation(qz - scene.k * math.sin(scene.theta))
        depth = np.exp((1j * qz[:, None] + mu[:, None]) * P[2][None, :])  # (N_QZ, n_atoms)
        s2 = ((g ** 2).sum(axis=0)[:, None] + qz[None, :] ** 2) / (16 * math.pi ** 2)

        A = np.zeros((g.shape[1], len(qz)), dtype=complex)
        for w_of, sites in _species(scene).items():
            cols = np.isin(idx, sites)
            if cols.any():
                A += w_of(s2) * (in_plane[:, cols] @ depth[:, cols].T)
        # The incident waves tilted up or down each read the rod a little higher or lower.
        profile = _smooth(np.abs(A) ** 2, spread.sigma_qz / (qz[1] - qz[0]))
        return _Lattice("rod", B, np.eye(2), ms.start, ns.start,
                        _index(use, ms, ns), profile.astype(np.float32))

    def _reconstruction(self, scene: Scene, B, Minv, strength, q_par, qz, reach) -> _Lattice:
        """The reconstruction's atoms are unknown, so its rods carry the top plane's
        own scattering, |sum f|^2, times `strength`: flat, without the slab's Bragg
        modulation, and falling off with q as the form factors do."""
        ms, ns = self._span(B, q_par, reach)
        c = _grid(ms, ns)
        mesh = (Minv @ c.T).T
        fractional = np.any(np.abs(mesh - np.round(mesh)) > 1e-6, axis=1)
        use = _limit(fractional & _reached(B, c, q_par, reach))
        g = B @ c[use].T
        s2 = ((g ** 2).sum(axis=0)[:, None] + qz[None, :] ** 2) / (16 * math.pi ** 2)
        top = set(scene.cell.sites[list(scene.cell.planes[scene.top].atoms)].tolist())
        F = np.zeros_like(s2)
        for w_of, sites in _species(scene).items():
            F += sum(1 for i in sites if i in top) * w_of(s2)
        return _Lattice("fractional", B, Minv, ms.start, ns.start,
                        _index(use, ms, ns), (strength * F ** 2).astype(np.float32))

    def _rod_spots(self, scene: Scene, lat: _Lattice, qz: np.ndarray) -> list[RheedSpot]:
        """Where the Ewald sphere crosses each rod: kx = k cos(theta) + gx, ky = gy,
        and kz is whatever makes |k_out| = k."""
        k, th = scene.k, scene.theta
        c = lat.coeffs()
        if len(c) == 0:
            return []
        gx, gy = lat.B @ c.T
        kx = k * math.cos(th) + gx
        kz2 = k * k - kx * kx - gy * gy
        ok = (kx > 0) & (kz2 > 0) & lat.table.any(axis=1)
        kz = np.sqrt(np.where(ok, kz2, 0.0))
        u, v = scene.screen.pixel_of(np.stack([kx, gy, kz]))
        sp = scene.screen.spec
        ok &= scene.screen.inside(u, v, 0.1 * max(sp.width_px, sp.height_px))
        # A superlattice puts thousands of rods on the screen; only those in view are
        # listed for it.
        if lat.kind == "fractional":
            ok &= scene.screen.inside(u, v)
        rows = np.flatnonzero(ok)
        qzi = kz[rows] + k * math.sin(th)
        # |A|^2 at the crossing, read linearly between the rod's samples.
        pos = np.clip((qzi - qz[0]) / (qz[1] - qz[0]), 0, len(qz) - 1.0001)
        lo = pos.astype(int)
        frac = pos - lo
        intensity = lat.table[rows, lo] * (1 - frac) + lat.table[rows, lo + 1] * frac
        mesh = (lat.to_mesh @ c[rows].T).T
        hkl = (scene.crystal.orth.T @ scene.R.T
               @ np.stack([gx[rows], gy[rows], np.zeros(len(rows))])).T / (2 * math.pi)
        repeat = scene.zone_repeat()
        spots = []
        for n, i in enumerate(rows):
            zone = None
            if repeat is not None:
                z = abs(gx[i] * repeat / (2 * math.pi))
                zone = round(z) if abs(z - round(z)) < 1e-3 else None
            spots.append(RheedSpot(
                kind=lat.kind,
                label=fraction_label(mesh[n]),
                indices=[_clean(x) for x in mesh[n]],
                hkl=[_clean(x) for x in hkl[n]],
                x_px=round(float(u[i]), 2), y_px=round(float(v[i]), 2),
                intensity=float(intensity[n]),
                laue_zone=zone,
                in_view=bool(scene.screen.inside(u[i], v[i])),
                q=[round(float(gx[i]), 5), round(float(gy[i]), 5), round(float(qzi[n]), 5)],
            ))
        return spots

    def _streak_maxima(self, scene: Scene, lat: _Lattice, qz: np.ndarray, spread: _Spread) -> list[RheedSpot]:
        """The bright points along wide streaks: each rod's |Psi|^2 maxima (its bulk
        Bragg points), placed where the Ewald sphere passes nearest them.

        At height q_z the sphere is a circle in the (k_out,x, k_out,y) plane, of radius
        sqrt(|k_out|^2 - k_out,z^2) with k_out,z = q_z + k_in,z. The rod sits at
        (k_in,x + g_x, g_y) there; the nearest point of the circle is along the same
        direction, and their distance d is how far off the rod's centre the sphere
        passes, measured against the rod's width that way (x and y differ, see
        _rod_spread). A maximum is listed while d < 3 sigma, weighted by exp(-d^2 / 2 sigma^2):
        narrow rods list only the maxima the Laue circle happens to cross, wide ones
        every Bragg point along the streak -- including rods the sphere never crosses
        at the centre.
        """
        K, k_in = scene.k, scene.k_in
        coeffs = lat.coeffs()
        if len(coeffs) == 0:
            return []
        prof = lat.table
        interior = (prof[:, 1:-1] > prof[:, :-2]) & (prof[:, 1:-1] >= prof[:, 2:])
        rows, cols = np.nonzero(interior)
        cols = cols + 1
        g = lat.B @ coeffs.T  # (2, n_rods), lab
        repeat = scene.zone_repeat()
        out = []
        for r, c in zip(rows.tolist(), cols.tolist()):
            # The peak between samples: the vertex of the parabola through three.
            lo, mid, hi = (float(x) for x in prof[r, c - 1:c + 2])
            curve = lo - 2 * mid + hi
            step = 0.5 * (lo - hi) / curve if curve < 0 else 0.0
            qzi = float(qz[c]) + step * float(qz[1] - qz[0])
            peak = mid - 0.25 * (lo - hi) * step
            kz = qzi + k_in[2]
            radius2 = K * K - kz * kz
            if kz <= 0 or radius2 <= 0:
                continue
            px, py = k_in[0] + g[0, r], g[1, r]
            norm = math.hypot(px, py)
            if norm == 0:
                continue
            d = abs(norm - math.sqrt(radius2))
            # (d / sigma)^2 with sigma the rod's width along the offset, (px, py) / norm.
            m2 = d * d * ((px / norm) ** 2 / spread.var_x + (py / norm) ** 2 / spread.var_y)
            if m2 > _REACH ** 2:
                continue
            kx, ky = px / norm * math.sqrt(radius2), py / norm * math.sqrt(radius2)
            if kx <= 0:
                continue
            # The bulk point this maximum belongs to: the rod's in-plane momentum plus q_z
            # along the normal, in the structure's own reciprocal cell.
            G_crystal = scene.R.T @ np.array([g[0, r], g[1, r], qzi])
            hkl = scene.crystal.orth.T @ G_crystal / (2 * math.pi)
            near = np.rint(hkl)
            if np.abs(hkl - near).max() > _BRAGG_TOL:
                continue
            u, v = scene.screen.pixel_of(np.array([kx, ky, kz]))
            u, v = float(u), float(v)
            if not scene.screen.inside(u, v):
                continue
            zone = None
            if repeat is not None:
                z = abs(g[0, r] * repeat / (2 * math.pi))
                zone = round(z) if abs(z - round(z)) < 1e-3 else None
            out.append(RheedSpot(
                kind="streak_max",
                label=" ".join(str(int(x)) for x in near),
                indices=[float(x) for x in near], hkl=[float(x) for x in near],
                x_px=round(u, 2), y_px=round(v, 2),
                intensity=peak * math.exp(-m2 / 2),
                laue_zone=zone, in_view=True,
                q=[round(kx - k_in[0], 5), round(ky, 5), round(qzi, 5)],
            ))
        return out

    # --- 3D islands ------------------------------------------------------------

    def _island_cov(self, scene: Scene) -> np.ndarray:
        """A transmission spot's (3, 3) covariance in q: the island size's in every
        direction, and the divergence's at right angles to k_in (see _rod_spread)."""
        m, b = scene.request.morphology, scene.request.beam
        size = 2 * math.pi / (m.island_nm * 10) / _FWHM
        tilt = scene.k * b.divergence_mrad * 1e-3 / _FWHM
        polar = np.array([math.sin(scene.theta), 0.0, math.cos(scene.theta)])
        return size ** 2 * np.eye(3) + tilt ** 2 * (np.outer(polar, polar) + np.diag([0.0, 1.0, 0.0]))

    def _excitation_var(self, scene: Scene, k_out: np.ndarray) -> np.ndarray:
        """Variance of a bulk point's excitation error |k_in + G| - |k_in|, which is
        its distance from the sphere along k_out."""
        n = k_out / np.linalg.norm(k_out, axis=0)
        return np.einsum("in,ij,jn->n", n, self._island_cov(scene), n)

    def _bulk_points(self, scene: Scene, q: np.ndarray, valid: np.ndarray) -> list[dict]:
        """Bulk reciprocal-lattice points within reach of the Ewald sphere, on screen."""
        if not valid.any():
            return []
        q_max = float(np.linalg.norm(q[:, valid], axis=0).max()) \
            + _REACH * math.sqrt(float(np.linalg.eigvalsh(self._island_cov(scene)).max()))
        cr = scene.crystal
        lim = [math.ceil(q_max * float(np.linalg.norm(cr.orth[:, i])) / (2 * math.pi)) for i in range(3)]
        if np.prod([2 * x + 1 for x in lim]) > 2_000_000:
            raise ValueError("the islands' reciprocal lattice is too dense to enumerate at this "
                             "screen size; lower islands to 0 or use a smaller cell")
        hkl = np.array(list(itertools.product(*(range(-x, x + 1) for x in lim))), dtype=float)
        G = scene.R @ cr.reciprocal @ hkl.T  # (3, n)
        k_out = scene.k_in[:, None] + G
        excitation = np.linalg.norm(k_out, axis=0) - scene.k
        var = self._excitation_var(scene, k_out)
        keep = (excitation ** 2 < _REACH ** 2 * var) & (k_out[2] > 0) & (np.linalg.norm(G, axis=0) > 0)
        if not keep.any():
            return []
        hkl, G, k_out, excitation, var = hkl[keep], G[:, keep], k_out[:, keep], excitation[keep], var[keep]
        u, v = scene.screen.pixel_of(k_out)
        s2 = (G ** 2).sum(axis=0) / (16 * math.pi ** 2)
        F = np.zeros(len(hkl), dtype=complex)
        morph = scene.request.morphology
        for site, f in zip(cr.sites, cr.frac):
            b_iso = site.b_iso if site.b_iso is not None else morph.debye_waller_b
            F += (site.occupancy * form_factor(site.element, s2) * np.exp(-b_iso * s2)
                  * np.exp(2j * math.pi * hkl @ f))
        out = []
        for n in range(len(hkl)):
            intensity = float(abs(F[n]) ** 2)
            if intensity < 1e-9 or not scene.screen.inside(u[n], v[n], 0.1 * scene.screen.spec.width_px):
                continue
            out.append({"hkl": hkl[n], "G": G[:, n], "I": intensity, "u": float(u[n]), "v": float(v[n]),
                        "excitation": float(excitation[n]), "var": float(var[n])})
        return out

    def _bulk_spots(self, scene: Scene, bulk: list[dict]) -> list[RheedSpot]:
        return [
            RheedSpot(
                kind="bulk", label=" ".join(str(int(x)) for x in b["hkl"]),
                indices=[float(x) for x in b["hkl"]], hkl=[float(x) for x in b["hkl"]],
                x_px=round(b["u"], 2), y_px=round(b["v"], 2),
                intensity=b["I"] * math.exp(-b["excitation"] ** 2 / (2 * b["var"])),
                in_view=bool(scene.screen.inside(b["u"], b["v"])),
                q=[round(float(x), 5) for x in b["G"]],
            )
            for b in bulk
        ]

    # --- the picture ---------------------------------------------------------------

    def _render(self, scene, q, valid, qz, lattices, spread, foot, bulk) -> np.ndarray:
        sp = scene.screen.spec
        req = scene.request
        H, W = sp.height_px, sp.width_px
        surface = np.zeros((H, W))
        islands = np.zeros((H, W))

        if req.morphology.islands < 1 and valid.any():
            q_par = q[:2][:, valid]
            dq = qz[1] - qz[0]
            qzi = np.clip(np.rint((q[2][valid] - qz[0]) / dq).astype(int), 0, len(qz) - 1)
            flat = np.zeros(q_par.shape[1])
            for lat in lattices:
                flat += _paint(lat, q_par, qzi, spread, foot[:, valid])
            surface[valid] = flat

        if req.morphology.islands > 0 and bulk:
            cov = self._island_cov(scene)
            s_i = math.sqrt(float(np.linalg.eigvalsh(cov).max()))
            # Half-width of a spot in pixels: its angular size on the screen, plus slack
            # for the stretch a grazing exit gives it.
            half = int(math.ceil(_REACH * s_i / scene.k * sp.camera_length_mm / sp.pixel_size_mm * 3)) + 2
            # Each pixel averages the spot over the q it covers: (H, W, 3, 3) covariances.
            du, dv = np.gradient(q, axis=2), np.gradient(q, axis=1)
            pix = (np.einsum("iyx,jyx->yxij", du, du) + np.einsum("iyx,jyx->yxij", dv, dv)) * _PIXEL_SIGMA ** 2
            det0 = float(np.linalg.det(cov))
            for b in bulk:
                u0, v0 = int(round(b["u"])), int(round(b["v"]))
                ys = slice(max(v0 - half, 0), min(v0 + half + 1, H))
                xs = slice(max(u0 - half, 0), min(u0 + half + 1, W))
                if ys.start >= ys.stop or xs.start >= xs.stop:
                    continue
                c = cov + pix[ys, xs]
                d = np.moveaxis(q[:, ys, xs] - b["G"][:, None, None], 0, -1)
                m2 = np.einsum("yxi,yxi->yx", d, np.linalg.solve(c, d[..., None])[..., 0])
                # sqrt(det cov / det c): what a pixel wider than the spot keeps of it.
                # (The pixel adds nothing along the sphere's normal, so c is not singular.)
                scale = np.sqrt(det0 / np.linalg.det(c))
                islands[ys, xs] += b["I"] * scale * np.exp(-m2 / 2) * valid[ys, xs]

        f = req.morphology.islands
        img = (1 - f) * _unit(surface) + f * _unit(islands)

        uu, vv = np.meshgrid(np.arange(W, dtype=float), np.arange(H, dtype=float))
        marks = scene.landmarks()
        if req.render.background > 0:
            su, sv = marks["specular_px"]
            w = 0.25 * H
            img += req.render.background * np.exp(-((uu - su) ** 2 + (vv - sv) ** 2) / (2 * w * w)) * valid
        if req.render.direct_beam:
            du, dv = marks["direct_beam_px"]
            w = max(2.0, 2 * req.render.blur_px)
            img += np.exp(-((uu - du) ** 2 + (vv - dv) ** 2) / (2 * w * w)) * ~valid

        if req.render.blur_px > 0:
            import cv2

            img = cv2.GaussianBlur(img, (0, 0), req.render.blur_px)
        img = _unit(img)
        if req.render.noise_counts > 0:
            rng = np.random.default_rng(req.render.seed)
            img = _unit(rng.poisson(img * req.render.noise_counts).astype(float))
        return img.astype(np.float32)


def _species(scene: Scene) -> dict:
    """Sites that scatter alike -- same element, occupancy and B -- grouped, so each
    form factor is evaluated once however many sites share it. Keys are callables
    s^2 -> occ * f(s) * exp(-B s^2); values the crystal.sites indices."""
    b_default = scene.request.morphology.debye_waller_b
    groups: dict[tuple, list[int]] = {}
    for i, site in enumerate(scene.crystal.sites):
        b_iso = site.b_iso if site.b_iso is not None else b_default
        groups.setdefault((site.element, site.occupancy, b_iso), []).append(i)
    return {_Weight(*key): idx for key, idx in groups.items()}


@dataclass(frozen=True)
class _Weight:
    element: str
    occupancy: float
    b_iso: float

    def __call__(self, s2: np.ndarray) -> np.ndarray:
        return self.occupancy * form_factor(self.element, s2) * np.exp(-self.b_iso * s2)


#: Rods one lattice may compute. Si(111)-7x7 across a wide screen is a few thousand.
MAX_RODS = 50_000


def _limit(use: np.ndarray) -> np.ndarray:
    if use.sum() > MAX_RODS:
        raise ValueError(f"{int(use.sum())} rods reach the screen (limit {MAX_RODS}); the "
                         "surface cell or reconstruction is very large for this screen")
    return use


def _grid(ms: range, ns: range) -> np.ndarray:
    """Every (m, n), m-major -- the order _index lays them out in."""
    m, n = np.meshgrid(np.arange(ms.start, ms.stop), np.arange(ns.start, ns.stop), indexing="ij")
    return np.stack([m.ravel(), n.ravel()], axis=1).astype(float)


def _index(use: np.ndarray, ms: range, ns: range) -> np.ndarray:
    index = np.full(len(use), -1, dtype=np.int32)
    index[use] = np.arange(int(use.sum()), dtype=np.int32)
    return index.reshape(len(ms), len(ns))


def _reached(B: np.ndarray, coeffs: np.ndarray, q_par: np.ndarray, reach: float) -> np.ndarray:
    """Which rods (rows of coeffs) come within `reach` of some visible pixel's q.

    The table spans a rectangle of lattice indices; on a skewed (hexagonal) mesh or
    along a wide screen most of it is never near the screen. Checked on every 4th
    pixel, with a lattice spacing of slack for the pixels skipped.
    """
    if q_par.size == 0:
        return np.zeros(len(coeffs), dtype=bool)
    sub = q_par[:, ::4]
    base = np.rint(np.linalg.solve(B, sub)).astype(np.int64)
    spacing = min(float(np.linalg.norm(B @ np.array(v))) for v in ((1, 0), (0, 1), (1, 1), (1, -1)))
    r = math.ceil(reach / spacing) + 1
    # (m, n) -> one int64, so the membership test is np.isin rather than Python sets.
    key = lambda m, n: m * 1_000_003 + n  # noqa: E731
    hit = np.unique(np.concatenate([
        np.unique(key(base[0] + dm, base[1] + dn))
        for dm, dn in itertools.product(range(-r, r + 1), repeat=2)
    ]))
    keys = np.rint(coeffs).astype(np.int64)
    return np.isin(key(keys[:, 0], keys[:, 1]), hit)


def _footprint(q: np.ndarray) -> np.ndarray:
    """The in-plane q each pixel covers, as a covariance: (3, H, W) of xx, xy, yy.

    A pixel is a small square on the screen, so a parallelogram in q spanned by
    dq/du and dq/dv. At 25 keV and the lab camera a pixel spans ~0.02 1/A across --
    wider than the rod of a good substrate, which sampled at the pixel's centre
    alone would show or vanish depending on where in the pixel it happens to fall.
    The square is taken as a Gaussian _PIXEL_SIGMA wide (see there).
    """
    du, dv = np.gradient(q[:2], axis=2), np.gradient(q[:2], axis=1)
    return np.stack([du[0] ** 2 + dv[0] ** 2, du[0] * du[1] + dv[0] * dv[1],
                     du[1] ** 2 + dv[1] ** 2]) * _PIXEL_SIGMA ** 2


def _smooth(table: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian smoothing along axis 1, sigma in samples; the ends held flat."""
    if sigma < 0.3:
        return table
    r = int(math.ceil(_REACH * sigma))
    w = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    w /= w.sum()
    padded = np.pad(table, ((0, 0), (r, r)), mode="edge")
    n = table.shape[1]
    return sum(w[i] * padded[:, i:i + n] for i in range(2 * r + 1))


def _paint(lat: _Lattice, q_par: np.ndarray, qzi: np.ndarray, spread: _Spread,
           foot: np.ndarray) -> np.ndarray:
    """Sum the lattice's rods over the pixels, each pixel visiting only the rods
    within reach of it (found from its own lattice coordinates, not by scanning
    every rod), and reading the rod's profile only where one is that close.

    A pixel sees a rod's Gaussian averaged over the q it covers (`foot`, from
    _footprint): the covariances add, and the peak drops by sqrt(det rod / det sum),
    so a rod narrower than a pixel is dimmer but never missed.
    """
    q_par = q_par.astype(np.float32)
    f = np.linalg.solve(lat.B, q_par)
    base = np.rint(f).astype(np.int32)
    cxx, cxy, cyy = spread.var_x + foot[0], foot[1], spread.var_y + foot[2]
    det = cxx * cyy - cxy * cxy
    ixx, ixy, iyy = (cyy / det).astype(np.float32), (-cxy / det).astype(np.float32), (cxx / det).astype(np.float32)
    scale = np.sqrt(spread.var_x * spread.var_y / det).astype(np.float32)
    r = math.ceil(_REACH * math.sqrt(float((cxx + cyy).max())) / lat.spacing())
    n_m, n_n = lat.index.shape
    B = lat.B.astype(np.float32)
    out = np.zeros(q_par.shape[1], dtype=np.float32)
    for dm, dn in itertools.product(range(-r, r + 1), repeat=2):
        m = base[0] + dm
        n = base[1] + dn
        dx = q_par[0] - (B[0, 0] * m + B[0, 1] * n)
        dy = q_par[1] - (B[1, 0] * m + B[1, 1] * n)
        m2 = ixx * dx * dx + 2 * ixy * dx * dy + iyy * dy * dy
        near = np.flatnonzero(m2 < _REACH ** 2)
        if near.size == 0:
            continue
        mi, ni = m[near] - lat.m0, n[near] - lat.n0
        ok = (mi >= 0) & (mi < n_m) & (ni >= 0) & (ni < n_n)
        row = lat.index[mi[ok], ni[ok]]
        near, row = near[ok][row >= 0], row[row >= 0]
        out[near] += lat.table[row, qzi[near]] * scale[near] * np.exp(-m2[near] / 2)
    return out


def _unit(a: np.ndarray) -> np.ndarray:
    top = float(a.max()) if a.size else 0.0
    return a / top if top > 0 else a


def _normalise(spots: list[RheedSpot]) -> None:
    """Each kind relative to its own strongest spot on screen."""
    for kind in KINDS:
        mine = [s for s in spots if s.kind == kind]
        top = max((s.intensity for s in mine if s.in_view), default=0.0) or \
            max((s.intensity for s in mine), default=0.0)
        for s in mine:
            s.intensity = round(s.intensity / top, 6) if top > 0 else 0.0
    spots.sort(key=lambda s: (KINDS.index(s.kind), -s.intensity))


def _clean(x: float) -> float:
    r = round(float(x))
    return float(r) if abs(x - r) < 1e-6 else round(float(x), 4)
