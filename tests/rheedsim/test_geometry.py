"""Where things land: checked against closed-form geometry, not against the code.

T0: the library called directly, no node.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

pytest.importorskip("gemmi")

from lumi.contracts.payloads.simulation import (  # noqa: E402
    BeamSpec,
    RheedSimRequest,
    ScreenSpec,
    StructureSpec,
    SurfaceSpec,
)
from lumi.rheedsim import StructureStore, simulate  # noqa: E402
from lumi.rheedsim.geometry import Screen, wavelength  # noqa: E402
from lumi.rheedsim.scene import build  # noqa: E402

SCREEN = ScreenSpec(camera_length_mm=300, pixel_size_mm=0.25, width_px=720, height_px=540,
                    origin_x_px=360, origin_y_px=480)


def req(name="SrTiO3", normal=(0, 0, 1), azimuth=(1, 0, 0), kev=20.0, deg=3.0, screen=SCREEN, **kw):
    return RheedSimRequest(structure=StructureSpec(name=name),
                           surface=SurfaceSpec(normal=list(normal), azimuth=list(azimuth), **kw),
                           beam=BeamSpec(energy_kev=kev, incidence_deg=deg), screen=screen)


def rods(meta, zone=None):
    return [s for s in meta.spots if s.kind == "rod" and s.in_view
            and (zone is None or s.laue_zone == zone)]


# --- the electron ------------------------------------------------------------------


@pytest.mark.parametrize("kev, angstrom", [(10, 0.12204), (20, 0.08588), (30, 0.06979), (100, 0.03701)])
def test_relativistic_wavelength(kev, angstrom):
    assert wavelength(kev) == pytest.approx(angstrom, abs=2e-5)


# --- the screen --------------------------------------------------------------------


@pytest.mark.parametrize("roll, fx, fy", [(0, False, False), (0, False, True), (180, True, False),
                                          (37.5, True, True), (-90, False, False)])
def test_screen_and_image_coordinates_invert(roll, fx, fy):
    s = Screen.resolve(SCREEN.model_copy(update={"roll_deg": roll, "flip_x": fx, "flip_y": fy}))
    u, v = np.meshgrid(np.linspace(-50, 800, 7), np.linspace(-20, 600, 5))
    y, z = s.to_screen(u, v)
    u2, v2 = s.to_image(y, z)
    assert np.allclose(u, u2) and np.allclose(v, v2)


def test_an_unset_origin_puts_the_horizon_near_the_edge_the_pattern_rises_from():
    up = Screen.resolve(ScreenSpec())
    assert up.origin == (360, pytest.approx(486))  # pattern rises towards row 0
    flipped = Screen.resolve(ScreenSpec(flip_y=True))
    assert flipped.origin == (360, pytest.approx(54))


def test_specular_and_direct_beam_sit_L_tan_theta_either_side_of_the_horizon():
    meta, _ = simulate(req(deg=2.5), image=False)
    rise = 300 * math.tan(math.radians(2.5)) / 0.25
    assert meta.specular_px == [360, pytest.approx(480 - rise, abs=0.01)]
    assert meta.direct_beam_px == [360, pytest.approx(480 + rise, abs=0.01)]
    (edge_a, edge_b) = meta.shadow_edge_px
    assert edge_a[1] == edge_b[1] == pytest.approx(480)


def test_the_zeroth_laue_zone_is_a_circle_through_the_specular_spot():
    meta, _ = simulate(req(deg=4.0), image=False)
    zero = rods(meta, zone=0)
    assert len(zero) >= 5
    radius = 300 * math.tan(math.radians(4.0)) / 0.25
    for s in zero:
        assert math.hypot(s.x_px - 360, s.y_px - 480) == pytest.approx(radius, abs=0.05), s.label


def test_streak_spacing_is_the_lattice_seen_down_the_beam():
    """Rod (0, n) of SrTiO3 (001) along [100]: the Ewald sphere meets it at
    k_out = (k cos(theta), 2 pi n / a, kz), which lands L * ky / kx across."""
    meta, _ = simulate(req(kev=15.0, deg=2.0), image=False)
    k = 2 * math.pi / wavelength(15.0)
    for s in rods(meta, zone=0):
        n = s.indices[1]
        expect = 360 - 300 * (2 * math.pi * n / 3.905) / (k * math.cos(math.radians(2))) / 0.25
        assert s.x_px == pytest.approx(expect, abs=0.02), s.label


def test_higher_energy_packs_the_streaks_closer():
    def spacing(kev):
        by = {s.label: s for s in rods(simulate(req(kev=kev), image=False)[0], zone=0)}
        return abs(by["0 1"].x_px - by["0 -1"].x_px)
    assert spacing(10) / spacing(30) == pytest.approx(wavelength(10) / wavelength(30), rel=1e-3)


def test_the_110_azimuth_spaces_streaks_by_the_diagonal():
    along_100 = {s.label: s for s in rods(simulate(req(), image=False)[0], zone=0)}
    along_110 = {s.label: s for s in rods(simulate(req(azimuth=(1, 1, 0)), image=False)[0], zone=0)}
    d100 = abs(along_100["0 1"].x_px - 360)
    # Along [110] the zeroth-zone rods are (-1, 1)-type, a factor sqrt2 further apart.
    first = min((s for s in along_110.values() if s.label != "0 0"), key=lambda s: abs(s.x_px - 360))
    assert abs(first.x_px - 360) == pytest.approx(d100 * math.sqrt(2), rel=2e-3)


def test_turning_the_sample_moves_the_pattern_and_drops_the_zone_labels():
    straight = simulate(req(), image=False)[0]
    turned = simulate(req(azimuth_offset_deg=2.0), image=False)[0]
    assert all(s.laue_zone is None for s in turned.spots)
    a = {s.label: s for s in rods(straight)}
    b = {s.label: s for s in rods(turned)}
    assert (a["0 0"].x_px, a["0 0"].y_px) == (b["0 0"].x_px, b["0 0"].y_px)  # specular stays
    # A rod slides along its own streak: the Ewald sphere cuts it at another height.
    assert a["0 1"].x_px == pytest.approx(b["0 1"].x_px, abs=0.1)
    assert abs(a["0 1"].y_px - b["0 1"].y_px) > 5


def test_an_azimuth_out_of_the_surface_is_refused():
    with pytest.raises(ValueError, match="does not lie in the"):
        simulate(req(azimuth=(0, 0, 1)), image=False)


def test_nothing_on_screen_says_so():
    meta, _ = simulate(req(deg=19.0, screen=SCREEN.model_copy(update={"camera_length_mm": 4000})),
                       image=False)
    assert any("nothing reaches the screen" in w for w in meta.warnings)


# --- labels ------------------------------------------------------------------------


def test_rod_labels_carry_the_bulk_indices():
    meta, _ = simulate(req(), image=False)
    for s in rods(meta):
        assert s.hkl == [s.indices[0], s.indices[1], 0.0]


def test_a_centred_lattice_gets_its_primitive_mesh():
    """fcc MgO (001): the surface cell is a/sqrt2, so a rod at bulk (1 0 0) --
    extinct in the bulk -- does not exist, and (1 1 0) is a first-order rod."""
    meta, _ = simulate(req(name="MgO"), image=False)
    assert meta.mesh.a1_length == pytest.approx(4.212 / math.sqrt(2), abs=1e-4)
    listed = [s for s in meta.spots if s.kind == "rod"]
    for s in listed:
        h, k, _ = s.hkl
        assert (h + k) % 2 == 0, s.hkl
    # Along [100] the zeroth zone is (0 2k); the (1 1) rods are the first zone.
    assert {tuple(s.hkl[:2]) for s in listed if s.laue_zone == 0} >= {(0, 2), (0, -2)}
    assert {s.laue_zone for s in listed if abs(s.hkl[0]) == 1} == {1}


def test_si_111_mesh_is_the_one_7x7_is_counted_in():
    meta, _ = simulate(req(name="Si", normal=(1, 1, 1), azimuth=(1, -1, 0)), image=False)
    assert meta.mesh.a1_length == pytest.approx(3.8403, abs=1e-3)
    assert meta.mesh.gamma_deg == pytest.approx(120)
    assert meta.mesh.layer_spacing == pytest.approx(5.431 / math.sqrt(3), abs=1e-3)


def test_the_zone_repeat_of_a_centred_lattice_is_its_shortest_row():
    scene = build(req(name="Si", azimuth=(1, 1, 0)), StructureStore(None).get("Si"), SCREEN)
    assert scene.zone_repeat() == pytest.approx(5.431 / math.sqrt(2))


def test_hexagonal_c_plane_mesh():
    meta, _ = simulate(req(name="Al2O3", azimuth=(1, 0, 0)), image=False)
    assert (meta.mesh.a1, meta.mesh.a2) == ([1, 0, 0], [0, 1, 0])
    assert meta.mesh.gamma_deg == pytest.approx(120)
    assert meta.mesh.a1_length == pytest.approx(4.7589)


def test_every_zone_is_evenly_spaced_across_the_screen():
    """Within a zone k_x = k cos(theta) + g_x is shared, so y = L g_y / k_x is linear in
    g_y: equal horizontal steps, each zone's a little wider than the one before."""
    meta, _ = simulate(req(), image=False)
    k = 2 * math.pi / wavelength(20.0)
    for zone in (0, 1):
        xs = sorted(s.x_px for s in rods(meta, zone=zone))
        steps = np.diff(xs)
        assert len(steps) >= 3
        expect = 300 * (2 * math.pi / 3.905) / (k * math.cos(math.radians(3)) - zone * 2 * math.pi / 3.905) / 0.25
        assert steps == pytest.approx(expect, abs=0.02), zone
