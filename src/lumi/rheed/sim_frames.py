"""The frames the simulated RHEED camera can show, and what each one is a picture of.

Each is the first frame of a real recording on the lab camera, so on the simulation
stack the live feed looks like the lab's -- and, because each comes with the scene it
shows (fitted at 25 keV, see docs/RHEED_SIMULATION.md §12), a simulated pattern can be
laid over it and line up. The origin differs between the two by the sample's height on
the day; the camera scale does not.

    python -m nodes.rheed --src simcam --sim-frame ysz
    scripts/start_simulation.sh --substrate ysz
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lumi.path import PROJECT_ROOT

ASSETS = Path(__file__).resolve().parent / "assets"


@dataclass(frozen=True)
class SimFrame:
    file: str
    description: str
    #: The scene it shows, as a simulation request would name it.
    structure: str
    normal: tuple[int, int, int]
    azimuth: tuple[int, int, int]
    energy_kev: float
    incidence_deg: float
    #: Shadow-edge centre on the image, px -- ScreenSpec.origin_x_px / origin_y_px.
    origin_px: tuple[float, float]

    @property
    def path(self) -> Path:
        return ASSETS / self.file


SIM_FRAMES: dict[str, SimFrame] = {
    "sto": SimFrame(
        file="sto_001.npy",
        description="SrTiO3(001), beam along [100], 700 degC -- first frame of "
                    "HZO-LSMO-022-L1 (2026-03-09)",
        structure="SrTiO3", normal=(0, 0, 1), azimuth=(1, 0, 0),
        energy_kev=25.0, incidence_deg=1.89, origin_px=(374.6, 129.7),
    ),
    "ysz": SimFrame(
        file="ysz_111.npy",
        description="YSZ(111), beam along [1-10], 805 degC -- first frame of "
                    "UMD_TFO_YSZ_AL_01 (2025-09-19)",
        structure="YSZ", normal=(1, 1, 1), azimuth=(1, -1, 0),
        energy_kev=25.0, incidence_deg=1.63, origin_px=(387.2, 179.4),
    ),
}

DEFAULT_SIM_FRAME = "sto"


def resolve(source: str) -> tuple[Path, SimFrame | None]:
    """A frame name ("sto") or a path to a .npy or image file."""
    if source in SIM_FRAMES:
        frame = SIM_FRAMES[source]
        return frame.path, frame
    path = Path(source)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"no sim frame {source!r}: not one of "
                                f"{', '.join(SIM_FRAMES)} and no such file {path}")
    return path, None
