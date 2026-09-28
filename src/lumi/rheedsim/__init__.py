"""RHEED pattern simulation: a crystal, a surface cut, a beam and a screen, to an image.

    from lumi.rheedsim import simulate
    from lumi.contracts.payloads.simulation import RheedSimRequest, StructureSpec

    meta, image = simulate(RheedSimRequest(structure=StructureSpec(name="SrTiO3")))

The request model is the node's wire payload too, so a notebook and the frontend
describe a simulation the same way. Kinematic only for now; `backend` is the seam a
dynamical (multislice / Bloch-wave) calculation plugs into, using the same Scene.
"""

from __future__ import annotations

import time
from typing import Protocol

import numpy as np

from lumi.contracts.payloads.simulation import RheedSimMeta, RheedSimRequest, RheedSpot, ScreenSpec

from .crystal import Crystal, from_cif, from_manual
from .kinematic import Kinematic
from .scene import Scene, build, summarize
from .store import StructureStore

__all__ = [
    "Backend", "Crystal", "Kinematic", "Scene", "StructureStore",
    "from_cif", "from_manual", "simulate", "summarize",
]


class Backend(Protocol):
    name: str

    def run(self, scene: Scene, *, image: bool) -> tuple[list[RheedSpot], np.ndarray | None]: ...


KINEMATIC = Kinematic()


def simulate(
    req: RheedSimRequest,
    *,
    store: StructureStore | None = None,
    crystal: Crystal | None = None,
    default_screen: ScreenSpec | None = None,
    image: bool = True,
    backend: Backend = KINEMATIC,
) -> tuple[RheedSimMeta, np.ndarray | None]:
    """The pattern and its labelled spots. `image=False` skips the picture (the spot
    list alone is milliseconds -- what an overlay or a fit wants)."""
    t0 = time.perf_counter()
    if crystal is None:
        crystal = (store or StructureStore(None)).resolve(req.structure)
    scene = build(req, crystal, req.screen or default_screen or ScreenSpec())
    spots, img = backend.run(scene, image=image)
    meta = RheedSimMeta(
        structure=scene.summary(), mesh=scene.mesh(), screen=scene.screen.filled(),
        wavelength_a=round(scene.lam, 6), k_inv_a=round(scene.k, 4),
        spots=spots, warnings=scene.warnings, **scene.landmarks(),
        elapsed_ms=round((time.perf_counter() - t0) * 1e3, 1),
    )
    return meta, img
