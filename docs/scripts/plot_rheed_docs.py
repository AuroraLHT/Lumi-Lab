"""Draw the figures in docs/RHEED_SIMULATION.md.

    .venv/bin/python docs/scripts/plot_rheed_docs.py        # writes docs/img/rheed_sim/*.png

Schematics that would be unreadable to scale say so on the figure. Everything else
(spot positions, the Ewald footprint, the rod profile, the simulated screens) is
computed by lumi.rheedsim itself, so the pictures cannot drift from the code without
this script showing it.
"""

from __future__ import annotations

import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Arc, FancyArrowPatch, Rectangle  # noqa: E402

from lumi.contracts.payloads.simulation import (  # noqa: E402
    BeamSpec,
    Morphology,
    RheedSimRequest,
    ScreenSpec,
    StructureSpec,
    SurfaceSpec,
)
from lumi.rheedsim import StructureStore, simulate  # noqa: E402
from lumi.rheedsim.geometry import Screen, wavelength  # noqa: E402
from lumi.rheedsim.kinematic import Kinematic  # noqa: E402
from lumi.rheedsim.scene import build  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "img" / "rheed_sim"

#: The lab camera's screen, as in cfg/settings.example.toml.
LAB = ScreenSpec(camera_length_mm=300, pixel_size_mm=0.25, width_px=720, height_px=540,
                 origin_x_px=268, origin_y_px=87, flip_y=True)
KEV, THETA = 20.0, 3.0
A_STO = 3.905

BEAM = "#d62728"
OUTC = "#1f77b4"
QC = "#2ca02c"
ROD = "#7f7f7f"


def arrow(ax, p, q, color, lw=2.0, style="-|>", ls="-", **kw):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle=style, mutation_scale=14, color=color,
                                 lw=lw, linestyle=ls, **kw))


def sto(**kw) -> RheedSimRequest:
    kw.setdefault("surface", SurfaceSpec())
    return RheedSimRequest(structure=StructureSpec(name="SrTiO3"),
                           beam=BeamSpec(energy_kev=KEV, incidence_deg=THETA), screen=LAB, **kw)


# --- 1. the lab frame, side view -----------------------------------------------------


def fig_geometry_side():
    th = math.radians(12)  # exaggerated so the angles can be seen
    L = 10.0
    fig, ax = plt.subplots(figsize=(9, 4.6))
    ax.add_patch(Rectangle((-3, -0.5), 6, 0.5, color="#c9b89a", zorder=1))
    ax.text(0, -0.3, "sample (surface z = 0)", ha="center", va="center", fontsize=9)
    ax.add_patch(Rectangle((L, -3.2), 0.18, 6.4, color="#88c988", zorder=1))
    ax.text(L + 0.35, 2.9, "phosphor\nscreen", fontsize=9, va="top")

    # incoming beam, specular, direct beam
    src = (-7, 7 * math.tan(th))
    arrow(ax, src, (0, 0), BEAM)
    ax.text(-6.4, 7 * math.tan(th) + 0.25, r"$\mathbf{k}_\mathrm{in}$", color=BEAM, fontsize=13)
    zs = L * math.tan(th)
    arrow(ax, (0, 0), (L, zs), OUTC)
    ax.text(L - 3.6, zs + 0.35, r"specular, $\mathbf{k}_\mathrm{out}$", color=OUTC, fontsize=11)
    arrow(ax, (0, 0), (L, -zs), BEAM, ls="--", lw=1.2)
    ax.text(L - 5.2, -zs - 0.5, "direct beam (past the sample)", color=BEAM, fontsize=9)

    ax.plot([0, L], [0, 0], color="k", lw=0.8, ls="--")
    ax.text(L / 2, 0.12, "horizon", fontsize=8, ha="center")
    ax.add_patch(Rectangle((L, -3.2), 0.18, 3.2, color="#333333", alpha=0.35, zorder=2))
    ax.text(L + 0.35, -1.6, "shadow\n(z < 0)", fontsize=9, va="center")

    ax.add_patch(Arc((0, 0), 5.0, 5.0, theta1=180 - 12, theta2=180, color=BEAM))
    ax.text(-2.9, 0.18, r"$\theta$", color=BEAM, fontsize=13)
    ax.add_patch(Arc((0, 0), 5.0, 5.0, theta1=0, theta2=12, color=OUTC))
    ax.text(2.6, 0.2, r"$\theta$", color=OUTC, fontsize=13)

    # dimensions
    ax.annotate("", (0, -2.3), (L, -2.3), arrowprops=dict(arrowstyle="<->", color="k"))
    ax.text(L / 2, -2.2, "camera length $L$", ha="center", fontsize=10)
    ax.annotate("", (L - 0.3, 0), (L - 0.3, zs), arrowprops=dict(arrowstyle="<->", color=OUTC))
    ax.text(L - 0.45, zs / 2, r"$L\tan\theta$", ha="right", va="center", fontsize=10, color=OUTC)
    ax.plot(L, 0, "ko", ms=5, zorder=5)
    ax.text(L + 0.35, 0.15, "origin $(u_0, v_0)$\n= shadow-edge centre", fontsize=9)

    # axes
    arrow(ax, (-6.5, -2.7), (-5.0, -2.7), "k", lw=1.2)
    arrow(ax, (-6.5, -2.7), (-6.5, -1.2), "k", lw=1.2)
    ax.text(-4.9, -2.85, "$x$ (beam direction along the surface)", fontsize=9)
    ax.text(-6.6, -1.1, "$z$ (surface normal)", fontsize=9)
    ax.plot(-6.5, -2.7, "o", mfc="white", mec="k", ms=7)
    ax.plot(-6.5, -2.7, "k.", ms=3)
    ax.text(-6.9, -3.25, r"$y$ (out of the page)", fontsize=8)

    ax.set_xlim(-7.5, 13.5)
    ax.set_ylim(-3.4, 3.4)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(r"Lab frame, side view (angles exaggerated: real $\theta$ is 1-5$^\circ$)")
    fig.tight_layout()
    fig.savefig(OUT / "geometry_side.png", dpi=150)
    plt.close(fig)


# --- 2. screen coordinates vs image pixels ---------------------------------------------


def fig_geometry_screen():
    meta, _ = simulate(sto(), image=False)
    scr = Screen.resolve(LAB)
    zero = [s for s in meta.spots if s.kind == "rod" and s.laue_zone == 0 and s.in_view]
    first = [s for s in meta.spots if s.kind == "rod" and s.laue_zone == 1 and s.in_view]
    L, th = LAB.camera_length_mm, math.radians(THETA)

    fig, (a, b) = plt.subplots(1, 2, figsize=(12, 5))
    # (a) on the screen, looking down the beam: +y to the left, +z up
    t = np.linspace(0, math.pi, 200)
    R = L * math.tan(th)
    a.plot(R * np.cos(t), R * np.sin(t), color=OUTC, lw=1, ls="--",
           label=r"Laue circle $L_0$: $y^2+z^2=L^2\tan^2\theta$")
    for group, mk, lab in ((zero, "o", "zone 0 rods"), (first, "s", "zone 1 rods")):
        yz = np.array([scr.to_screen(s.x_px, s.y_px) for s in group])
        a.plot(yz[:, 0], yz[:, 1], mk, color=OUTC if mk == "o" else "#9467bd", ms=6, label=lab)
        for s, (y, z) in zip(group, yz):
            if mk == "o" and abs(s.indices[1]) <= 3:
                r = math.hypot(y, z) or 1.0
                # a's x axis is reversed, so +y points left on the page
                a.annotate(s.label, (y, z), textcoords="offset points", xytext=(-14 * y / r - 6, 12 * z / r),
                           fontsize=8)
    a.axhspan(-40, 0, color="#333333", alpha=0.25)
    a.text(0, -8, "shadow: $k_{\\mathrm{out},z} < 0$", ha="center", fontsize=9)
    a.plot(0, 0, "ko")
    a.plot(0, -R, "x", color=BEAM, ms=9, mew=2, label=r"direct beam $(0,-L\tan\theta)$")
    a.axhline(0, color="k", lw=0.8)
    a.axvline(0, color="k", lw=0.4, ls=":")
    a.set_xlim(70, -70)  # +y is to the left, seen down the beam
    a.set_ylim(-25, 75)
    a.set_aspect("equal")
    a.set_xlabel(r"$y$ on the screen (mm)  $\leftarrow$ +y")
    a.set_ylabel(r"$z$ on the screen (mm)")
    a.set_title("(a) Screen, seen looking down the beam")
    a.legend(fontsize=8, loc="upper right")

    # (b) the same spots in image pixels, the lab camera (flip_y)
    for group, mk in ((zero, "o"), (first, "s")):
        a_ = np.array([[s.x_px, s.y_px] for s in group])
        b.plot(a_[:, 0], a_[:, 1], mk, color=OUTC if mk == "o" else "#9467bd", ms=6)
        for s in group:
            if mk == "o" and s.label != "0 0" and abs(s.indices[1]) <= 3:
                du, dv = s.x_px - scr.origin[0], s.y_px - scr.origin[1]
                r = math.hypot(du, dv) or 1.0
                # points run up the page while rows run down it, hence -dv
                b.annotate(s.label, (s.x_px, s.y_px), textcoords="offset points",
                           xytext=(16 * du / r - 6, -14 * dv / r - 3), fontsize=8)
    u0, v0 = scr.origin
    tt = np.linspace(0, 2 * math.pi, 300)
    rp = R / LAB.pixel_size_mm
    b.plot(u0 + rp * np.cos(tt), v0 + rp * np.sin(tt), color=OUTC, lw=1, ls="--")
    b.axhspan(0, v0, color="#333333", alpha=0.25)
    b.plot(u0, v0, "ko")
    b.annotate("origin $(u_0, v_0)$", (u0, v0), xytext=(u0 + 40, v0 - 35), fontsize=9,
               arrowprops=dict(arrowstyle="->"))
    sx, sy = meta.specular_px
    b.annotate("specular", (sx, sy), xytext=(sx + 70, sy + 50), fontsize=9, arrowprops=dict(arrowstyle="->"))
    b.set_xlim(0, LAB.width_px)
    b.set_ylim(LAB.height_px, 0)  # row 0 at the top, like an image
    b.set_aspect("equal")
    b.set_xlabel("column $u$ (px)")
    b.set_ylabel("row $v$ (px)")
    b.set_title("(b) Image pixels, lab camera (flip_y: upside down)")
    fig.suptitle(f"SrTiO$_3$(001) along [100], {KEV:g} keV, $\\theta$ = {THETA:g}$^\\circ$, "
                 f"$L$ = {L:g} mm, $p$ = {LAB.pixel_size_mm} mm/px")
    fig.tight_layout()
    fig.savefig(OUT / "geometry_screen.png", dpi=150)
    plt.close(fig)


# --- 3. the Ewald sphere against the rods, side view -------------------------------------


def fig_ewald_side():
    fig, (a, b) = plt.subplots(1, 2, figsize=(13, 5.4), gridspec_kw={"width_ratios": [1.1, 1]})

    # (a) schematic: k shrunk to 5 rod spacings so the sphere's curvature shows
    bsp, k, th = 1.0, 5.0, math.radians(14)
    C = np.array([-k * math.cos(th), k * math.sin(th)])
    t = np.linspace(0, 2 * math.pi, 400)
    a.plot(C[0] + k * np.cos(t), C[1] + k * np.sin(t), color=OUTC, lw=1.5)
    for m in range(-5, 3):
        a.axvline(m * bsp, color=ROD, lw=3 if m == 0 else 1.6, alpha=0.6, zorder=0)
        a.text(m * bsp, -1.75, f"{m}", ha="center", fontsize=8, color=ROD)
    a.text(-6.0, -2.35, r"rods at $g_x = m\,b$, $g_y = 0$ (vertical lines)", fontsize=8, color=ROD)
    arrow(a, tuple(C), (0, 0), BEAM)
    a.text(C[0] / 2 - 0.2, C[1] / 2 - 0.55, r"$\mathbf{k}_\mathrm{in}$", color=BEAM, fontsize=13)
    a.plot(*C, "ko", ms=4)
    a.text(C[0] - 0.4, C[1] + 0.25, r"$C = -\mathbf{k}_\mathrm{in}$", fontsize=10)
    a.plot(0, 0, "ks", ms=6)
    a.text(0.12, -0.45, "O (000)", fontsize=9)
    # crossings with the rods, upper half only (k_out,z > 0)
    for m in range(-5, 3):
        gx = m * bsp
        kx = k * math.cos(th) + gx
        if 0 < kx < k:  # kx <= 0 heads back towards the source: it never reaches the screen
            kz = math.sqrt(k * k - kx * kx)
            qz = kz + k * math.sin(th)
            a.plot(gx, qz, "o", color=QC, ms=7, zorder=5)
            if m == -2:
                arrow(a, tuple(C), (gx, qz), OUTC)
                a.text(C[0] / 2 + gx / 2 - 0.9, (C[1] + qz) / 2 + 0.1, r"$\mathbf{k}_\mathrm{out}$",
                       color=OUTC, fontsize=13)
                arrow(a, (0, 0), (gx, qz), QC)
                a.text(gx / 2 + 0.15, qz / 2, r"$\mathbf{q}$", color=QC, fontsize=13)
    a.text(-4.9, 5.9, r"Ewald sphere: $|\mathbf{k}_\mathrm{out}| = |\mathbf{k}_\mathrm{in}|$", color=OUTC, fontsize=10)
    a.set_xlim(-6.2, 2.6)
    a.set_ylim(-2.5, 6.6)
    a.set_aspect("equal")
    a.set_xlabel(r"$q_x$ (along the beam)")
    a.set_ylabel(r"$q_z$ (along the normal)")
    a.set_title(r"(a) Where the sphere crosses the rods (not to scale: $|\mathbf{k}_\mathrm{in}|$ = 5 $b$)")

    # (b) to scale for SrTiO3 at 20 keV: the sphere is nearly flat and nearly
    # tangent to the rods, so a rod of width sigma is cut over a long stretch of q_z
    k = 2 * math.pi / wavelength(KEV)
    th = math.radians(THETA)
    bsp = 2 * math.pi / A_STO
    C = np.array([-k * math.cos(th), k * math.sin(th)])
    qx = np.linspace(-4.5, 1.5, 6000)
    root = k * k - (qx - C[0]) ** 2
    qz_up = np.where(root >= 0, C[1] + np.sqrt(np.maximum(root, 0)), np.nan)
    sigma = math.hypot(2 * math.pi / 50, k * 0.3e-3) / (2 * math.sqrt(2 * math.log(2)))  # T = 5 nm
    for m in range(-2, 1):
        b.axvspan(m * bsp - 3 * sigma, m * bsp + 3 * sigma, color=ROD, alpha=0.18, lw=0)
        b.axvline(m * bsp, color=ROD, lw=1.2)
        b.text(m * bsp, 0.25, f"$m$={m}", ha="center", fontsize=8, color=ROD)
        # The stretch of the sphere inside the rod's width: that stretch is the streak.
        cut = np.abs(qx - m * bsp) < 3 * sigma
        b.plot(qx[cut], qz_up[cut], color=QC, lw=5, alpha=0.8, solid_capstyle="butt",
               label="sphere inside a rod: the streak" if m == 0 else None)
    b.plot(qx, qz_up, color=OUTC, lw=1.6,
           label="Ewald sphere, $|\\mathbf{k}_\\mathrm{in}| = |\\mathbf{k}_\\mathrm{out}|$"
                 f" = {k:.1f} Å$^{{-1}}$")
    b.axhline(k * math.sin(th), color="k", lw=0.7, ls="--")
    b.text(-4.4, k * math.sin(th) - 0.9, r"horizon: $k_{\mathrm{out},z}=0$, so $q_z = -k_{\mathrm{in},z}$", fontsize=8)
    for m in range(-2, 1):
        gx = m * bsp
        kx = k * math.cos(th) + gx
        if 0 < kx < k:
            qz = math.sqrt(k * k - kx * kx) + k * math.sin(th)
            b.plot(gx, qz, "o", color="k", ms=4, zorder=5)
    b.set_xlim(-4.5, 1.5)
    b.set_ylim(0, 30)
    b.set_xlabel(r"$q_x$ (Å$^{-1}$)")
    b.set_ylabel(r"$q_z$ (Å$^{-1}$)")
    b.set_title("(b) To scale: SrTiO$_3$ [100], 20 keV, 3$^\\circ$.\n"
                "Shaded: rod width ($\\pm3\\sigma$) for 5 nm terraces")
    b.legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(OUT / "ewald_side.png", dpi=150)
    plt.close(fig)


# --- 4. the Ewald sphere seen from above: which rods it reaches ---------------------------


def fig_ewald_top():
    meta, _ = simulate(sto(), image=False)
    k = 2 * math.pi / wavelength(KEV)
    th = math.radians(THETA)
    bsp = 2 * math.pi / A_STO
    fig, ax = plt.subplots(figsize=(11, 6))
    m, n = np.meshgrid(np.arange(-3, 2), np.arange(-9, 10), indexing="ij")
    gx, gy = m * bsp, n * bsp
    kx = k * math.cos(th) + gx
    inside = kx ** 2 + gy ** 2 < k * k
    ax.plot(gx[~inside], gy[~inside], "o", mfc="white", mec=ROD, ms=6, label="rod the sphere misses")
    ax.plot(gx[inside], gy[inside], "o", color=ROD, ms=6, label="rod the sphere crosses")
    seen = [s for s in meta.spots if s.kind == "rod" and s.in_view]
    ax.plot([s.q[0] for s in seen], [s.q[1] for s in seen], "o", mfc="none", mec=QC, ms=11, mew=2,
            label="...and lands on the lab screen")
    gyy = np.linspace(-16, 16, 400)
    ax.plot(np.sqrt(k * k - gyy ** 2) - k * math.cos(th), gyy, color=OUTC, lw=1.5,
            label="edge: $k_{\\mathrm{out},z} = 0$, i.e.\n"
                  "$(k_{\\mathrm{in},x} + g_x)^2 + g_y^2 = |\\mathbf{k}_\\mathrm{out}|^2$")
    for zone in range(0, 4):
        ax.axvline(-zone * bsp, color="#9467bd", lw=0.8, ls=":")
        ax.text(-zone * bsp + 0.05, 15.2, f"$L_{zone}$", color="#9467bd", fontsize=10)
    arrow(ax, (-4.5, -14.5), (-2.8, -14.5), BEAM)
    ax.text(-4.5, -13.8, "beam", color=BEAM, fontsize=9)
    ax.set_xlabel(r"$g_x$ (Å$^{-1}$, along the beam)")
    ax.set_ylabel(r"$g_y$ (Å$^{-1}$)")
    ax.set_xlim(-5.4, 1.9)
    ax.set_ylim(-16, 16.5)
    ax.set_title("Rods seen from above: SrTiO$_3$(001) along [100], 20 keV, 3$^\\circ$.\n"
                 "Laue zones $L_n$ are the rows of rods across the beam.")
    ax.legend(fontsize=8, loc="upper left", bbox_to_anchor=(1.01, 1.0))
    fig.tight_layout()
    fig.savefig(OUT / "ewald_top.png", dpi=150)
    plt.close(fig)


# --- 5. simulated screens: spots vs streaks --------------------------------------------------


def fig_screens():
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.2))
    for ax, T in zip(axes, (200.0, 4.0)):
        meta, img = simulate(sto(morphology=Morphology(terrace_nm=T)))
        ax.imshow(np.sqrt(img), cmap="gray", vmin=0, vmax=1)
        u0, v0 = meta.origin_px
        rp = LAB.camera_length_mm * math.tan(math.radians(THETA)) / LAB.pixel_size_mm
        tt = np.linspace(0, math.pi, 200)
        ax.plot(u0 + rp * np.cos(tt), v0 + rp * np.sin(tt), color=OUTC, lw=0.8, ls="--")
        (x0, y0), (x1, y1) = meta.shadow_edge_px
        ax.plot([x0, x1], [y0, y1], color="#ffbf00", lw=0.8)
        ax.plot(u0, v0, "o", color="#ffbf00", ms=4)
        zero = [s for s in meta.spots if s.kind == "rod" and s.in_view and s.laue_zone == 0]
        if T > 50:
            # Narrow rods: the sphere lights each only where it crosses the centre, on
            # the Laue circle -- so the 2D rod index is the right label, at that point.
            for s in zero:
                du, dv = s.x_px - u0, s.y_px - v0
                r = math.hypot(du, dv) or 1.0
                ax.annotate(s.label, (s.x_px, s.y_px), xytext=(s.x_px + 38 * du / r, s.y_px + 38 * dv / r),
                            color="#ffbf00", fontsize=8, ha="center", va="center",
                            arrowprops=dict(arrowstyle="-", color="#ffbf00", lw=0.6, shrinkB=3))
            ax.set_title(f"terrace $T$ = {T:g} nm: spots on the Laue circle, labelled by rod $(m\\,n)$",
                         fontsize=10)
        else:
            # Wide rods: each streak is labelled by its rod above the shadow edge, and its
            # bright points by the bulk (0 k l) they are -- they are off the Laue circle.
            cols = {int(s.indices[1]): s.x_px for s in zero}
            step = abs(cols.get(1, u0 + 26.4) - u0)
            for kk in range(-3, 4):
                ax.text(u0 - kk * step, v0 - 14, f"(0 {kk})", color="white", fontsize=7, ha="center")
            # (0 k) runs right to left here: +y is to the left seen down the beam, and the
            # lab camera only flips the picture vertically.
            # The simulator lists these itself: kind "streak_max", labelled by bulk hkl.
            marks = [x for x in meta.spots if x.kind == "streak_max" and x.laue_zone == 0
                     and x.intensity > 0.05]
            rows: dict[int, list[float]] = {}
            for x in marks:
                ax.plot(x.x_px, x.y_px, "o", mfc="none", mec="#ffbf00", ms=9, mew=0.9)
                rows.setdefault(int(x.hkl[2]), []).append(x.y_px)
            for ll, vs in sorted(rows.items()):
                ax.text(158, float(np.mean(vs)), f"$l$ = {ll}", color="#ffbf00", fontsize=8, va="center")
            ax.text(158, v0 - 14, "rod:", color="white", fontsize=7)
            ax.set_title(f"terrace $T$ = {T:g} nm: streaks. Circles: the spot list's streak maxima,\n"
                         "bulk $(0\\,k\\,l)$ with $k$ from the rod above and $l$ from the row label", fontsize=10)
        ax.set_xlim(150, 390)
        ax.set_ylim(200, 55)
        ax.set_xlabel("column $u$ (px)")
        ax.set_ylabel("row $v$ (px)")
    fig.suptitle("Simulated lab screen, SrTiO$_3$(001) [100], 20 keV, 3$^\\circ$ (sqrt grey scale). "
                 "Yellow line: shadow edge; blue dashes: Laue circle $L_0$")
    fig.tight_layout()
    fig.savefig(OUT / "screens.png", dpi=150)
    plt.close(fig)


# --- 6. |A|^2 along the specular rod ----------------------------------------------------------


def fig_rod_profile():
    fig, ax = plt.subplots(figsize=(9, 4.4))
    for term, color in (("TiO2", "#1f77b4"), ("SrO", "#ff7f0e")):
        r = sto(surface=SurfaceSpec(termination=term))
        scene = build(r, StructureStore(None).get("SrTiO3"), LAB)
        qz = scene.k * math.sin(scene.theta) + np.linspace(0.05, 14, 4000)
        (lat,) = Kinematic()._lattices(scene, np.zeros((2, 1)), qz, 0.01)
        prof = lat.table[lat.index[-lat.m0, -lat.n0]]
        ax.semilogy(qz, prof, color=color, lw=1.3, label=f"{term}-terminated")
    for order in range(3, 12):
        ax.axvline(2 * math.pi * order / A_STO, color="k", lw=0.6, ls=":")
        ax.text(2 * math.pi * order / A_STO, 0.98, f"$l$={order}", ha="center", va="top", fontsize=8,
                transform=ax.get_xaxis_transform())
    ks = scene.k * math.sin(scene.theta)
    ax.axvspan(0, ks, color="#333333", alpha=0.2)
    ax.text(ks / 2, 1e-1, "wave would leave\ninto the crystal", ha="center", fontsize=8)
    ax.set_xlim(0, qz[-1])
    ax.set_ylim(top=ax.get_ylim()[1] * 8)
    ax.set_xlabel(r"$q_z$ (Å$^{-1}$)")
    ax.set_ylabel(r"$|A(\mathbf{0}, q_z)|^2$ (Å$^2$)")
    ax.set_title(r"Specular rod of SrTiO$_3$(001), 20 keV, $\Lambda$ = 10 nm. "
                 r"Dotted: Bragg condition $q_z = 2\pi l/a$", fontsize=10)
    ax.legend(fontsize=9, loc="lower left")
    fig.tight_layout()
    fig.savefig(OUT / "rod_profile.png", dpi=150)
    plt.close(fig)


# --- 7. why a spot can vanish: where the sphere crosses each rod ------------------------------


def _rod_profiles(theta_deg: float, qz: np.ndarray | None = None):
    """|Psi|^2 along rods (0 0), (0 1), (0 2) of SrTiO3 (001) along [100], and the q_z at
    which the Ewald sphere crosses each (NaN when it does not)."""
    r = RheedSimRequest(structure=StructureSpec(name="SrTiO3"),
                        beam=BeamSpec(energy_kev=KEV, incidence_deg=theta_deg), screen=LAB)
    scene = build(r, StructureStore(None).get("SrTiO3"), LAB)
    k_in = scene.k_in
    if qz is None:
        qz = -k_in[2] + np.linspace(0.02, 10, 3000)
    b = 2 * math.pi / A_STO
    (lat,) = Kinematic()._lattices(scene, np.array([[0.0, 0, 0], [0, b, 2 * b]]), qz, 0.01)
    out = []
    for n in (0, 1, 2):
        prof = lat.table[lat.index[-lat.m0, n - lat.n0]]
        kz2 = k_in[2] ** 2 - (n * b) ** 2  # zone 0: k_out,x = k_in,x
        cross = math.sqrt(kz2) - k_in[2] if kz2 > 0 else float("nan")
        out.append((n, prof, cross))
    return qz, out


def fig_rod_crossings():
    colors = {0: "#1f77b4", 1: "#d62728", 2: "#2ca02c"}
    fig, (a, b) = plt.subplots(1, 2, figsize=(14, 4.8), gridspec_kw={"width_ratios": [1.25, 1]})

    qz, rods = _rod_profiles(THETA)
    for n, prof, cross in rods:
        a.semilogy(qz, prof, color=colors[n], lw=1.3, label=f"rod (0 {n})")
        a.plot(cross, np.interp(cross, qz, prof), "o", color=colors[n], ms=9, mec="k", zorder=5)
    for order in range(3, 8):
        a.axvline(2 * math.pi * order / A_STO, color="k", lw=0.6, ls=":")
        a.text(2 * math.pi * order / A_STO, 0.98, f"$l$={order}", ha="center", va="top", fontsize=8,
               transform=a.get_xaxis_transform())
    a.set_xlim(qz[0], 12.5)
    a.set_ylim(top=a.get_ylim()[1] * 6)
    a.set_xlabel(r"$q_z$ (Å$^{-1}$)")
    a.set_ylabel(r"$|\Psi(\mathbf{g}, q_z)|^2$ (Å$^2$)")
    a.set_title(f"(a) Along each rod at $\\theta$ = {THETA:g}$^\\circ$. "
                "Dots: where the Ewald sphere crosses", fontsize=10)
    a.legend(fontsize=9, loc="lower left")

    thetas = np.linspace(1.5, 6.0, 181)
    series = {n: [] for n in (0, 1, 2)}
    for th in thetas:
        qz_t, rods_t = _rod_profiles(float(th))
        for n, prof, cross in rods_t:
            series[n].append(np.interp(cross, qz_t, prof) if cross == cross else np.nan)
    for n in (0, 1, 2):
        b.semilogy(thetas, series[n], color=colors[n], lw=1.4, label=f"spot (0 {n})")
    b.axvline(THETA, color="k", lw=0.8, ls="--")
    b.text(THETA + 0.05, 0.02, f"{THETA:g}$^\\circ$ (a, Fig. 6)", transform=b.get_xaxis_transform(),
           fontsize=8)
    b.set_xlabel(r"incidence $\theta$ ($^\circ$)")
    b.set_ylabel(r"$|\Psi|^2$ at the crossing (Å$^2$)")
    b.set_title("(b) The same crossings as the incidence angle changes", fontsize=10)
    b.legend(fontsize=9)
    fig.suptitle(f"SrTiO$_3$(001) along [100], {KEV:g} keV, $\\Lambda$ = 10 nm, zeroth Laue zone")
    fig.tight_layout()
    fig.savefig(OUT / "rod_crossings.png", dpi=150)
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for f in (fig_geometry_side, fig_geometry_screen, fig_ewald_side, fig_ewald_top,
              fig_screens, fig_rod_profile, fig_rod_crossings):
        f()
        print("wrote", OUT / f"{f.__name__.removeprefix('fig_')}.png")


if __name__ == "__main__":
    main()
