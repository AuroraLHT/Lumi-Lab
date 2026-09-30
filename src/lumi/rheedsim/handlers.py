"""Serves `simulation.rheed_sim`: the simulator, and the structures it simulates.

Every simulation runs in a thread -- it is numpy-bound for a tenth of a second or
more, and the node's event loop also answers heartbeats.
"""

from __future__ import annotations

import asyncio

import numpy as np

from lumi.contracts.payloads.common import Ack, Empty
from lumi.contracts.payloads.simulation import (
    RheedJpegMeta,
    RheedJpegRequest,
    RheedSimMeta,
    RheedSimReadout,
    RheedSimRequest,
    SaveStructure,
    ScreenSpec,
    StructureInfo,
    StructureList,
    StructureName,
)

from . import DEFAULT_ENERGY_KEV, simulate, summarize
from .crystal import Crystal
from .library import BUILTINS
from .store import StructureStore


class RheedSimHandler:
    def __init__(self, store: StructureStore, screen: ScreenSpec | None = None,
                 energy_kev: float = DEFAULT_ENERGY_KEV) -> None:
        self.store = store
        #: The lab camera's screen, used when a request names none.
        self.screen = screen or ScreenSpec()
        #: The lab's usual beam energy, used when a request names none.
        self.energy_kev = energy_kev

    # --- structures -----------------------------------------------------------

    async def list_structures(self, req: Empty) -> StructureList:
        def work():
            out = []
            for name in self.store.names():
                try:
                    out.append(summarize(self.store.get(name)))
                except Exception:  # a hand-edited file should not hide the others
                    continue
            return StructureList(structures=out)
        return await asyncio.to_thread(work)

    async def get_structure(self, req: StructureName) -> StructureInfo:
        return _info(await asyncio.to_thread(self.store.get, req.name))

    async def save_structure(self, req: SaveStructure) -> StructureInfo:
        return _info(await asyncio.to_thread(self.store.save, req))

    async def delete_structure(self, req: StructureName) -> Ack:
        await asyncio.to_thread(self.store.delete, req.name)
        return Ack()

    # --- simulation --------------------------------------------------------------

    async def rheed_spots(self, req: RheedSimRequest) -> RheedSimMeta:
        meta, _ = await asyncio.to_thread(self._run, req, False)
        return meta

    async def simulate_rheed(self, req: RheedSimRequest) -> tuple[RheedSimMeta, np.ndarray]:
        return await asyncio.to_thread(self._run, req, True)

    async def simulate_rheed_jpeg(self, req: RheedJpegRequest) -> tuple[RheedJpegMeta, bytes]:
        def work():
            meta, image = self._run(req, True)
            return RheedJpegMeta(**meta.model_dump(), scale=req.scale), to_jpeg(image, req.scale, req.quality)
        return await asyncio.to_thread(work)

    def _run(self, req: RheedSimRequest, image: bool):
        return simulate(req, store=self.store, default_screen=self.screen,
                        default_energy_kev=self.energy_kev, image=image)

    def readout(self) -> RheedSimReadout:
        return RheedSimReadout(
            n_builtin=len(BUILTINS), n_saved=len(self.store.saved_names()),
            structures_path=str(self.store.root) if self.store.root else None,
        )


def to_jpeg(image: np.ndarray, scale: str, quality: int) -> bytes:
    import cv2

    if scale == "sqrt":
        image = np.sqrt(image)
    elif scale == "log":
        # Three decades onto the grey range: 1e-3 of the brightest is black.
        image = np.clip(1 + np.log10(np.maximum(image, 1e-3)) / 3, 0, 1)
    ok, buf = cv2.imencode(".jpg", (np.clip(image, 0, 1) * 255).astype(np.uint8),
                           [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return buf.tobytes()


def _info(crystal: Crystal) -> StructureInfo:
    return StructureInfo(**summarize(crystal).model_dump(), sites=list(crystal.sites))
