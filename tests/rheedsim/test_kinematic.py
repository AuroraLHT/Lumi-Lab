"""Intensities: what kinematic theory says, and what the picture shows.

T0: the library called directly, no node.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

pytest.importorskip("gemmi")

from lumi.contracts.payloads.simulation import (  # noqa: E402
    BeamSpec,
    Morphology,
    Reconstruction,
    RenderSpec,
    RheedSimRequest,
    ScreenSpec,
    StructureSpec,
    SurfaceSpec,
)
from lumi.rheedsim import StructureStore, simulate  # noqa: E402
from lumi.rheedsim.kinematic import Kinematic, _Spread  # noqa: E402
from lumi.rheedsim.scene import build  # noqa: E402

SCREEN = ScreenSpec(camera_length_mm=300, pixel_size_mm=0.25, width_px=400, height_px=300,
                    origin_x_px=200, origin_y_px=280)


def req(name="SrTiO3", surface=None, morphology=None, render=None, deg=3.0):
    return RheedSimRequest(structure=StructureSpec(name=name), surface=surface or SurfaceSpec(),
                           beam=BeamSpec(energy_kev=20.0, incidence_deg=deg), screen=SCREEN,
                           morphology=morphology or Morphology(),
                           render=render or RenderSpec(background=0, direct_beam=False, blur_px=0))


def test_the_specular_rod_peaks_at_the_bragg_condition():
    """Along (0 0) of SrTiO3 (001), |A|^2 peaks where qz = 2 pi l / a.

    qz starts at k sin(theta) ~ 3.8 1/A at 3 deg (below it the wave would leave into
    the crystal). Just above that the exit is so grazing that absorption leaves only
    the top plane, which flattens the profile -- so the test starts at l = 4. And
    only even l: for odd l the planes scatter against each other, F = f_Sr - f_Ti - f_O,
    which nearly cancels for electrons -- a neighbour's tail outshines it.
    """
    r = req()
    scene = build(r, StructureStore(None).get("SrTiO3"), SCREEN)
    qz = scene.k * math.sin(scene.theta) + np.linspace(0.3, 8.0, 3000)
    (lat,) = Kinematic()._lattices(scene, np.zeros((2, 1)), qz, _Spread(1e-4, 1e-4, 0.0), 0.03)
    profile = lat.table[lat.index[-lat.m0, -lat.n0]]
    for order in (4, 6):
        bragg = 2 * math.pi * order / 3.905
        near = np.abs(qz - bragg) < 0.8
        peak = qz[near][np.argmax(profile[near])]
        assert peak == pytest.approx(bragg, abs=0.03), order
        # ...and a Bragg peak outshines the anti-Bragg point halfway to the next.
        mid = np.argmin(np.abs(qz - (bragg + math.pi / 3.905)))
        assert profile[near].max() > 3 * profile[mid]


def test_the_termination_changes_intensities_not_positions():
    ti = simulate(req(surface=SurfaceSpec(termination="TiO2")), image=False)[0]
    sr = simulate(req(surface=SurfaceSpec(termination="SrO")), image=False)[0]
    assert (ti.mesh.termination, sr.mesh.termination) == ("TiO2", "SrO")
    # Rod crossings: which weak streak maxima clear the threshold is itself intensity.
    a = {s.label: s for s in ti.spots if s.kind == "rod"}
    b = {s.label: s for s in sr.spots if s.kind == "rod"}
    assert a.keys() == b.keys()
    assert all(a[k].x_px == b[k].x_px and a[k].y_px == b[k].y_px for k in a)
    # The top plane is one of ~18 the beam sees, so it nudges rather than transforms.
    assert any(abs(a[k].intensity - b[k].intensity) > 1e-3 for k in a)


def test_a_missing_termination_lists_the_ones_there_are():
    with pytest.raises(ValueError, match="0: TiO2, 1: SrO"):
        simulate(req(surface=SurfaceSpec(termination="LaO")), image=False)


def test_a_2x1_reconstruction_adds_half_order_rods_between_the_integer_ones():
    plain = simulate(req(), image=False)[0]
    assert not [s for s in plain.spots if s.kind == "fractional"]
    meta = simulate(req(surface=SurfaceSpec(azimuth=[0, 1, 0], reconstructions=[
        Reconstruction(matrix=[[2, 0], [0, 1]])])), image=False)[0]
    half = [s for s in meta.spots if s.kind == "fractional"]
    assert half and all(s.indices[0] % 1 == 0.5 and s.indices[1] % 1 == 0 for s in half)
    assert any(s.label == "1/2 0" or s.label == "-1/2 0" for s in half)


def test_islands_give_transmission_spots_with_the_bulk_extinctions():
    meta = simulate(req(name="MgO", morphology=Morphology(islands=1.0)), image=False)[0]
    bulk = [s for s in meta.spots if s.kind == "bulk" and s.in_view]
    assert bulk
    for s in bulk:  # fcc: h, k, l all even or all odd
        assert len({int(x) % 2 for x in s.hkl}) == 1, s.hkl


def test_the_image_is_dark_in_the_shadow_and_bright_on_the_rods():
    meta, img = simulate(req())
    assert img.shape == (300, 400) and img.dtype == np.float32
    assert img.max() == pytest.approx(1.0)
    assert img[285:, :].max() < 1e-6  # below the horizon, with the direct beam off
    strongest = max((s for s in meta.spots if s.in_view), key=lambda s: s.intensity)
    y, x = int(round(strongest.y_px)), int(round(strongest.x_px))
    assert img[y - 3:y + 4, x - 3:x + 4].max() > 0.5


def test_smaller_terraces_make_longer_streaks():
    def streak_rows(nm):
        meta, img = simulate(req(morphology=Morphology(terrace_nm=nm)))
        s = next(s for s in meta.spots if s.label == "0 1")
        column = img[:, int(round(s.x_px))]
        return int((column > 0.2 * column.max()).sum())
    assert streak_rows(3) > 2 * streak_rows(200)


def test_noise_is_seeded():
    r = req(render=RenderSpec(noise_counts=50, seed=7, background=0, direct_beam=False))
    a, b = simulate(r)[1], simulate(r)[1]
    assert np.array_equal(a, b)


def test_wide_streaks_list_their_bragg_maxima_as_bulk_points():
    """With small terraces the bright points of a streak sit at bulk (0 k l), off the
    Laue circle, and follow the structure factor: odd k at odd l, even k at even l."""
    r = req(morphology=Morphology(terrace_nm=4))
    meta, img = simulate(r)
    maxima = {s.label: s for s in meta.spots
              if s.kind == "streak_max" and s.laue_zone == 0 and s.intensity > 0.05}
    assert {"0 0 4", "0 1 5", "0 -1 5", "0 2 4", "0 -2 4", "0 1 3", "0 3 3"} <= maxima.keys()
    for s in maxima.values():
        k, order = int(s.hkl[1]), int(s.hkl[2])
        assert k % 2 == order % 2, s.label
        # ...and each is where the picture is actually bright.
        y, x = int(round(s.y_px)), int(round(s.x_px))
        assert img[y - 2:y + 3, x - 1:x + 2].max() > 0.5 * s.intensity * img.max()
    # (0 0 4) is off the Laue circle: nearer the shadow edge than the specular spot.
    specular = next(s for s in meta.spots if s.kind == "rod" and s.label == "0 0")
    assert abs(maxima["0 0 4"].y_px - specular.y_px) > 5


def test_narrow_rods_list_only_maxima_the_laue_circle_crosses():
    wide = simulate(req(morphology=Morphology(terrace_nm=4)), image=False)[0]
    narrow = simulate(req(morphology=Morphology(terrace_nm=500)), image=False)[0]
    count = lambda m: sum(s.kind == "streak_max" for s in m.spots)  # noqa: E731
    assert count(narrow) < count(wide)
    # The (0 3) rod is never crossed at its centre: only a wide rod shows it at all.
    assert not any(s.kind == "rod" and s.label == "0 3" for s in wide.spots)
    assert any(s.kind == "streak_max" and s.hkl[1] == 3 for s in wide.spots)
    assert not any(s.kind == "streak_max" and s.hkl[1] == 3 for s in narrow.spots)


def test_a_composition_two_planes_share_is_refused():
    """YSZ(111) has an O plane above the cations and one below: same composition,
    different surfaces. A name cannot pick one; the index can."""
    def ysz(termination):
        return req(name="YSZ", surface=SurfaceSpec(normal=[1, 1, 1], azimuth=[1, -1, 0],
                                                   termination=termination))
    meta = simulate(ysz(None), image=False)[0]
    oxygen = [i for i, c in enumerate(meta.mesh.terminations) if c.startswith("O")]
    assert len(oxygen) == 2
    with pytest.raises(ValueError, match="names 2 planes .*give the index"):
        simulate(ysz(meta.mesh.terminations[oxygen[0]]), image=False)
    assert simulate(ysz(oxygen[1]), image=False)[0].mesh.termination.startswith("O")
    cation = next(c for c in meta.mesh.terminations if c.startswith("Zr"))
    assert simulate(ysz(cation), image=False)[0].mesh.termination == cation


def _window(img, s, rows=15, cols=6):
    y, x = int(round(s.y_px)), int(round(s.x_px))
    return img[max(y - rows, 0):y + rows + 1, max(x - cols, 0):x + cols + 1]


@pytest.mark.parametrize("terrace_nm", [50, 1000])
def test_mirror_spots_are_equally_bright_wherever_they_fall_in_a_pixel(terrace_nm):
    """Rods narrower than a pixel: (0 1) and (0 -1) land at different places within
    their pixels (the origin is off-centre), yet carry the same intensity."""
    screen = SCREEN.model_copy(update={"origin_x_px": 200.37})
    r = req(morphology=Morphology(terrace_nm=terrace_nm)).model_copy(update={
        "screen": screen, "beam": BeamSpec(energy_kev=20.0, incidence_deg=3.0, divergence_mrad=0.05)})
    meta, img = simulate(r)
    rods = {s.label: s for s in meta.spots if s.kind == "rod"}
    left, right = _window(img, rods["0 1"]).sum(), _window(img, rods["0 -1"]).sum()
    assert left == pytest.approx(right, rel=0.01)
    # ...and the specular spot is there at all, however narrow its rod.
    assert _window(img, rods["0 0"]).max() > 0.3 * rods["0 0"].intensity


def test_divergence_blurs_spots_rather_than_making_streaks():
    """A divergent beam tilts k_in, which moves q across the rods and along them, but
    hardly along the beam: the spots grow round, not into streaks, and the
    streak's Bragg points (the transmission look) do not appear."""
    def extent(div):
        meta, img = simulate(req(morphology=Morphology(terrace_nm=200, mean_free_path_nm=10))
                             .model_copy(update={"beam": BeamSpec(energy_kev=20.0, incidence_deg=3.0,
                                                                  divergence_mrad=div)}))
        s = next(s for s in meta.spots if s.label == "0 1" and s.kind == "rod")
        column = img[:, int(round(s.x_px))]
        row = img[int(round(s.y_px)), :]
        return int((column > 0.2 * column.max()).sum()), int((row > 0.2 * row.max()).sum())
    tall, wide = extent(4.0)
    assert tall < 3 * wide
    assert tall < 3 * extent(0.05)[0] + 20
