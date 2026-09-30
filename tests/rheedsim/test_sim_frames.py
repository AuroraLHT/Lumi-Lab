"""The simulated camera's frames, and the scenes they are said to show.

Each frame is a real one from the lab camera, stored with the scene it was fitted to.
If the frame, its scene, or the lab screen in settings drift apart, the simulated spots
stop landing on the picture's -- which is what the frontend's overlay relies on.

T0: no broker.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.ndimage import maximum_filter

pytest.importorskip("gemmi")

from lumi.config import settings  # noqa: E402
from lumi.contracts.payloads.simulation import (  # noqa: E402
    BeamSpec,
    RheedSimRequest,
    ScreenSpec,
    StructureSpec,
    SurfaceSpec,
)
from lumi.rheed.sim_frames import DEFAULT_SIM_FRAME, SIM_FRAMES, resolve  # noqa: E402
from lumi.rheedsim import simulate  # noqa: E402


def lab_screen(frame) -> ScreenSpec:
    """The lab screen from settings, with this frame's shadow edge -- what the
    simulation node uses when started with --sim-frame."""
    cfg = {k.lower(): v for k, v in dict(settings.simulation.rheed.screen).items()}
    return ScreenSpec(**cfg).model_copy(update={"origin_x_px": frame.origin_px[0],
                                                "origin_y_px": frame.origin_px[1]})


@pytest.mark.parametrize("name", list(SIM_FRAMES))
def test_every_frame_is_a_lab_camera_frame(name):
    image = np.load(SIM_FRAMES[name].path)
    assert image.shape == (540, 720) and image.dtype == np.uint16
    assert image.max() <= 4095  # the camera is 12-bit


@pytest.mark.parametrize("name", list(SIM_FRAMES))
def test_simulated_spots_land_on_the_frames_spots(name):
    frame = SIM_FRAMES[name]
    meta, _ = simulate(RheedSimRequest(
        structure=StructureSpec(name=frame.structure),
        surface=SurfaceSpec(normal=list(frame.normal), azimuth=list(frame.azimuth)),
        beam=BeamSpec(energy_kev=frame.energy_kev, incidence_deg=frame.incidence_deg),
        screen=lab_screen(frame),
    ), image=False)
    crossings = [s for s in meta.spots if s.kind == "rod" and s.in_view]
    assert len(crossings) >= 3  # the specular and the two first-order spots
    image = np.load(frame.path).astype(float)
    peaks = maximum_filter(image, size=9)  # the brightest pixel within 4 px
    background = np.median(image[int(frame.origin_px[1]) + 20:, :])
    for s in crossings:
        y, x = int(round(s.y_px)), int(round(s.x_px))
        assert peaks[y, x] > 2.5 * background, (name, s.label, s.x_px, s.y_px)
    # ...and the shadow edge is where the picture turns dark.
    y0 = int(round(frame.origin_px[1]))
    assert np.median(image[y0 - 25:y0 - 10]) < 0.3 * np.median(image[y0 + 10:y0 + 25])


def test_resolve_takes_a_name_or_a_path(tmp_path):
    path, frame = resolve("ysz")
    assert frame is SIM_FRAMES["ysz"] and path.name == "ysz_111.npy"
    own = tmp_path / "mine.npy"
    np.save(own, np.zeros((4, 4), dtype=np.uint16))
    assert resolve(str(own)) == (own, None)
    with pytest.raises(FileNotFoundError, match="sto, ysz"):
        resolve("gaas")


def test_the_default_is_a_known_frame():
    assert DEFAULT_SIM_FRAME in SIM_FRAMES
    assert settings.rheed.simcam.source in SIM_FRAMES
