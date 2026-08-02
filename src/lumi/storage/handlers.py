"""Storage handler: records the other nodes' streams into HDF5.

Storage is the odd node out -- an "equipment" with no equipment. It is a *consumer* of
RHEED and the chamber, which has two consequences the old design papered over:

1. It can be perfectly healthy and still unable to record, because a source is down.
   The old code discovered that by calling get_state() on a client and getting a
   timeout, midway through starting a recording. Now it asks the monitor first, and
   reports `deps_available` in its own state.

2. Its metadata comes from other nodes' states. The old code read those as raw dicts
   (`camera_state["frame_dims"]`, `detection_state["classifier_classes"]`), so a
   renamed key was a KeyError at the worst possible moment. Those states are typed now.

The HDF5 machinery itself (Recorder, RecorderServer) is untouched -- it was never the
problem.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from lumi.config import settings
from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.storage import StorageReadout, StorageRequest, StorageStatus
from lumi.storage.record import Recorder, RecorderConfig, RecorderServer, RecorderServerConfig

log = logging.getLogger(__name__)

#: What each save option needs to be up before a recording can start.
DEPENDENCIES = {
    "save_frame": "rheed",
    "save_integration": "rheed",
    "save_ai": "detection",
    "save_log": "chamber",
}


class StorageHandler:
    def __init__(self, sources, registry_client=None, *, root_folder: str | None = None) -> None:
        """`sources` holds the generated clients this node records from."""
        self.sources = sources
        self.registry = registry_client
        self.root_folder = root_folder or settings.storage.hdf5_recorder.database_path

        self.recorder: Recorder | None = None
        self.recorder_server: RecorderServer | None = None
        self.project_name: str | None = None
        self.path: str | None = None
        self._subscribed: list = []
        #: Last known liveness of each source, refreshed from the registry. readout()
        #: is synchronous (the heartbeat calls it), so it cannot query the bus itself.
        self._deps: dict[str, bool] = {name: False for name in set(DEPENDENCIES.values())}

    # --- ops ---------------------------------------------------------------

    async def start_recording(self, req: StorageRequest) -> StorageStatus:
        if self.recorder_server is not None:
            return StorageStatus(
                ok=False,
                message=f"already recording {self.project_name!r}; call stop_recording first",
            )

        missing = await self._missing_dependencies(req)
        if missing:
            # Say *why* rather than failing opaquely halfway through a growth.
            return StorageStatus(
                ok=False,
                message=f"cannot record: {', '.join(missing)} not available on the bus",
            )

        try:
            config = await self._build_config(req)
        except Exception as exc:
            log.exception("could not assemble the recorder config")
            return StorageStatus(ok=False, message=f"metadata error: {exc}")

        try:
            self.recorder = Recorder(config=config)
        except FileExistsError as exc:
            return StorageStatus(
                ok=False,
                message=f"{exc}. Pass force_rewrite=true to overwrite.",
                project_name=req.project_name,
            )

        self.recorder_server = RecorderServer(
            RecorderServerConfig(), self.recorder, name="recorder_server"
        )
        created = self.recorder_server.create_datasets()
        if not created["succ"]:
            self.recorder_server = None
            self.recorder = None
            return StorageStatus(
                ok=False,
                message=f"could not create datasets: {created.get('error_message', '')}",
            )
        self.recorder_server.start()

        await self._subscribe(req)

        self.project_name = req.project_name
        self.path = str(Path(self.root_folder) / f"{req.project_name}.hdf5")
        log.info("recording %s", self.project_name)
        return StorageStatus(ok=True, message="recording", project_name=self.project_name, path=self.path)

    async def stop_recording(self, req: Empty) -> StorageStatus:
        if self.recorder_server is None:
            return StorageStatus(ok=False, message="not recording")

        await self._unsubscribe()

        name = self.project_name
        self.recorder_server.stop()
        self.recorder_server.join(timeout=10)
        self.recorder_server.close_storages()
        self.recorder_server = None
        self.recorder = None
        self.project_name = None

        log.info("stopped recording %s", name)
        return StorageStatus(ok=True, message="stopped", project_name=name, path=self.path)

    def readout(self) -> StorageReadout:
        return StorageReadout(
            is_storing=self.recorder_server is not None,
            project_name=self.project_name,
            path=self.path,
            deps_available=dict(self._deps),
        )

    # --- dependencies -------------------------------------------------------

    async def refresh_deps(self) -> None:
        """Update which sources are on the bus. Driven by a loop in nodes/storage.py."""
        up = await self._equipment_up()
        if up is None:
            return  # monitor unreachable; keep the last known picture rather than lying
        self._deps = {name: name in up for name in self._deps}

    async def _equipment_up(self) -> set[str] | None:
        """The set of live equipment, or None if the monitor did not answer."""
        if self.registry is None:
            return None

        try:
            # codegen drops the request argument for ops whose request type is Empty,
            # so this takes no payload. Passing one raised a TypeError that the except
            # below swallowed, which made every dependency look permanently down.
            listing = await self.registry.list_nodes()
        except Exception as exc:
            log.warning("registry unreachable: %s", exc, exc_info=True)
            return None
        return {n.equipment for n in listing.nodes if n.status == "up"}

    async def _missing_dependencies(self, req: StorageRequest) -> list[str]:
        """Which of the sources this request needs are not on the bus.

        Crucially, "the monitor did not answer" is NOT the same as "the sources are
        down". If the monitor is unreachable we proceed and say so: a dead monitor must
        never be the reason a growth goes unrecorded. The monitor is an observer, not
        an authority.
        """
        if self.registry is None:
            return []

        needed = {
            equipment
            for option, equipment in DEPENDENCIES.items()
            if getattr(req, option, False)
        }
        if not needed:
            return []

        up = await self._equipment_up()
        if up is None:
            log.warning(
                "the monitor did not answer -- recording anyway rather than letting a "
                "dead monitor stop the experiment"
            )
            return []

        self._deps = {name: name in up for name in self._deps}
        return sorted(needed - up)

    # --- recorder config ----------------------------------------------------

    async def _build_config(self, req: StorageRequest) -> RecorderConfig:
        frame_dim = frame_meta_columns = None
        log_columns = None
        pattern_dim = detection_meta_columns = None
        classifier_classes = detector_classes = None

        if req.save_frame:
            camera = await self.sources["camera"].get_state()
            frame_dim = camera.frame_dims
            frame_meta_columns = list((camera.frame_metas or {}) or [])
            if not frame_dim or len(frame_dim) < 2:
                raise ValueError(f"the camera reports an unusable frame size: {frame_dim}")

        if req.save_log:
            chamber = await self.sources["log"].get_state()
            log_columns = list(getattr(chamber, "columns", []) or [])

        if req.save_ai:
            detection = await self.sources["detection"].get_state()
            classifier_classes = list(detection.classifier_classes or [])
            detector_classes = list(getattr(detection, "detector_classes", []) or [])
            pattern_dim = frame_dim

        cfg = settings.storage.hdf5_recorder
        return RecorderConfig(
            project_name=req.project_name,
            root_folder=self.root_folder,
            frame_dim=frame_dim,
            frame_meta_columns=frame_meta_columns,
            log_columns=log_columns,
            pattern_dim=pattern_dim,
            detection_meta_columns=detection_meta_columns,
            classifier_classes=classifier_classes,
            detector_classes=detector_classes,
            initial_size=cfg.initial_size,
            save_frame=req.save_frame,
            save_log=req.save_log,
            save_ai=req.save_ai,
            save_integration=req.save_integration,
            frame_speed_limit=5,
            detection_speed_limit=5,
            force_rewrite=req.force_rewrite,
            compression="lzf",
            compression_opts=None,
            scaleoffset=0,
        )

    # --- subscriptions ------------------------------------------------------

    async def _subscribe(self, req: StorageRequest) -> None:
        if req.save_frame:
            camera = self.sources["camera"]

            async def on_frame(meta, frame) -> None:
                self.recorder_server.save_frame(frame, meta.model_dump())

            await camera.on_frame(on_frame)
            await self._ensure_streaming(camera)
            self._subscribed.append(camera)

        if req.save_log:
            chamber_log = self.sources["log"]

            async def on_log(entry, _payload) -> None:
                self.recorder_server.save_log(chamber_log=entry.values)

            await chamber_log.on_log(on_log)
            await self._ensure_streaming(chamber_log)
            self._subscribed.append(chamber_log)

        if req.save_integration:
            integrator = self.sources["integrator"]

            async def on_integration(sample, _payload) -> None:
                self.recorder_server.save_integrations(
                    integrations={k: v.model_dump() for k, v in sample.results.items()},
                    integrations_meta=sample.header.model_dump(),
                )

            await integrator.on_integration(on_integration)
            await self._ensure_streaming(integrator)
            self._subscribed.append(integrator)

        if req.save_ai:
            detection = self.sources["detection"]

            async def on_detection(result, arrays) -> None:
                self.recorder_server.save_prediction(**_prediction(result, arrays))

            await detection.on_detection(on_detection)
            await self._ensure_streaming(detection)
            self._subscribed.append(detection)

    async def _ensure_streaming(self, client) -> None:
        """The stream flag is global to the server, so only ask if it is not already on."""
        try:
            state = await client.get_state()
            if not getattr(state, "is_streaming", False):
                await client.start_streaming()
        except Exception:
            log.warning("could not start streaming on %s", client.target, exc_info=True)

    async def _unsubscribe(self) -> None:
        for client in self._subscribed:
            try:
                await client.unsubscribe_stream()
            except Exception:
                log.debug("unsubscribe failed for %s", client.target, exc_info=True)
        self._subscribed.clear()


def _prediction(result, arrays: dict[str, np.ndarray]) -> dict:
    """The detection contract -> the recorder's save_prediction arguments.

    The arrays now arrive as a named NPZ archive rather than base64 blobs inside the
    JSON, so this is a lookup rather than a decode.
    """
    boxes = result.boxes
    if boxes:
        masks = np.stack([arrays[b.mask_ref] for b in boxes if b.mask_ref]) if any(
            b.mask_ref for b in boxes
        ) else np.array([])
        bboxes = np.stack([[b.x, b.y, b.x + b.width, b.y + b.height] for b in boxes])
        labels = np.stack([b.label for b in boxes])
        scores = np.stack([b.score for b in boxes])
    else:
        masks = bboxes = labels = scores = np.array([])

    return {
        "pattern": arrays.get(result.pattern_ref) if result.pattern_ref else None,
        "masks": masks,
        "bboxes": bboxes,
        "labels": labels,
        "scores": scores,
        "cls_result": result.classification,
        "tracking": result.region2tracks,
        "detection_meta": {
            "time": result.time,
            "time_stamp": result.time_stamp,
            "uuid": result.uuid,
        },
    }
