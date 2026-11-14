"""Storage handler: records the other nodes' streams into HDF5 (format v2).

Storage is the odd node out -- an "equipment" with no equipment. It is a *consumer* of
RHEED and the chamber, which has two consequences the old design papered over:

1. It can be perfectly healthy and still unable to record, because a source is down.
   It asks the monitor first, and reports `deps_available` in its own state.

2. Its metadata comes from other nodes' states, which are typed.

A recording is a lumi.storage.v2 file. At start it snapshots what every source says
about itself (camera settings, integration boxes, the detector and its crop, the
chamber's full state, the experiment context the caller passes) into /meta; while it
runs it adds what changes to /events -- chamber events, camera settings, the detection
crop, and whatever the experiment driver reports with add_recording_event.

The HDF5 writes run on one worker thread: h5py serialises every call anyway, and
one thread keeps the writes in arrival order while the event loop stays free.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from lumi.config import settings
from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.pld import PldEventQuery
from lumi.contracts.payloads.storage import (
    ArchiveReadout,
    ListRecordings,
    RecordingDetection,
    RecordingDetectionQuery,
    RecordingDetections,
    RecordingDetectionsQuery,
    RecordingEventIn,
    RecordingEvents,
    RecordingEventsQuery,
    RecordingFrameMeta,
    RecordingFrameQuery,
    RecordingInfo,
    RecordingIntegration,
    RecordingIntegrationQuery,
    RecordingJpegMeta,
    RecordingJpegQuery,
    RecordingList,
    RecordingLog,
    RecordingLogQuery,
    RecordingName,
    RecordingSummary,
    StorageReadout,
    StorageRequest,
    StorageStatus,
)
from lumi.storage.archive import RecordingArchive, iso
from lumi.storage.record import SpeedLimiter
from lumi.storage.v2 import DetectionSchema, LogSchema, RecordingWriter

log = logging.getLogger(__name__)

def default_energy_kev() -> float:
    """The lab's usual RHEED beam energy (`rheed.energy_kev`); 25 keV if unset."""
    return float(settings.get("rheed.energy_kev", 25.0) or 25.0)


#: What each save option needs to be up before a recording can start.
DEPENDENCIES = {
    "save_frame": "rheed",
    "save_integration": "rheed",
    "save_ai": "detection",
    "save_log": "chamber",
}

#: Frames and detections kept per second. The camera runs at 5-30 fps; a growth is
#: judged on seconds, and every frame kept is 0.4 MB.
FRAME_RATE_LIMIT = 5.0
DETECTION_RATE_LIMIT = 5.0
#: Writes queued behind the worker past which frames and detections are dropped (and
#: counted) rather than queued. Log rows, integrations and events are never dropped.
MAX_PENDING = 64
#: How often buffered rows are pushed to the file, so a live reader -- and a crash --
#: sees them.
FLUSH_EVERY_S = 1.0
#: How often the sources are asked what changed while recording.
WATCH_EVERY_S = 2.0
#: A snapshot query that takes longer is left out of the metadata.
SNAPSHOT_TIMEOUT_S = 3.0


class StorageHandler:
    def __init__(self, sources, registry_client=None, *, root_folder: str | None = None) -> None:
        """`sources` holds the generated clients this node records from."""
        self.sources = sources
        self.registry = registry_client
        self.root_folder = root_folder or settings.storage.hdf5_recorder.database_path

        self.writer: RecordingWriter | None = None
        self.project_name: str | None = None
        self.path: str | None = None
        self._subscribed: list = []
        self._tasks: list[asyncio.Task] = []
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._pending = 0
        self._pending_lock = threading.Lock()
        self.dropped = {"frames": 0, "detections": 0}
        self._crop = None
        self._frame_limit: SpeedLimiter | None = None
        self._detection_limit: SpeedLimiter | None = None
        #: Last known liveness of each source, refreshed from the registry. readout()
        #: is synchronous (the heartbeat calls it), so it cannot query the bus itself.
        self._deps: dict[str, bool] = {name: False for name in set(DEPENDENCIES.values())}

    # --- ops ---------------------------------------------------------------

    async def start_recording(self, req: StorageRequest) -> StorageStatus:
        if self.writer is not None:
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
            log.exception("could not assemble the recording's layout")
            return StorageStatus(ok=False, message=f"metadata error: {exc}")

        path = Path(self.root_folder) / f"{req.project_name}.hdf5"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.writer = await asyncio.to_thread(
                RecordingWriter, path, overwrite=req.force_rewrite, **config)
        except FileExistsError as exc:
            return StorageStatus(
                ok=False,
                message=f"{exc}. Pass force_rewrite=true to overwrite.",
                project_name=req.project_name,
            )
        except Exception as exc:
            log.exception("could not create %s", path)
            return StorageStatus(ok=False, message=f"could not create the recording: {exc}",
                                 project_name=req.project_name)

        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="storage-write")
        self._pending = 0
        self.dropped = {"frames": 0, "detections": 0}
        self._frame_limit = SpeedLimiter(FRAME_RATE_LIMIT)
        self._detection_limit = SpeedLimiter(DETECTION_RATE_LIMIT)
        self.project_name = req.project_name
        self.path = str(path)
        self._write(self.writer.add_event, time.time(), "recording_started",
                    f"recording {req.project_name}",
                    {k: getattr(req, k) for k in ("save_frame", "save_ai", "save_log", "save_integration")})

        await self._subscribe(req)
        self._tasks = [asyncio.create_task(self._flush_loop(), name="storage-flush"),
                       asyncio.create_task(self._watch_loop(config["meta"]), name="storage-watch")]
        log.info("recording %s", self.project_name)
        return StorageStatus(ok=True, message="recording", project_name=self.project_name, path=self.path)

    async def stop_recording(self, req: Empty) -> StorageStatus:
        if self.writer is None:
            return StorageStatus(ok=False, message="not recording")

        await self._unsubscribe()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

        name, writer, executor = self.project_name, self.writer, self._executor
        dropped = dict(self.dropped)
        if any(dropped.values()):
            self._write(writer.add_event, time.time(), "dropped",
                        "writes fell behind; these were not kept", dropped)
        self._write(writer.add_event, time.time(), "recording_stopped", f"recording {name}")
        # Every queued write lands before the file is closed.
        executor.shutdown(wait=True)
        await asyncio.to_thread(writer.close)
        self.writer = None
        self._executor = None
        self.project_name = None

        log.info("stopped recording %s (%s)", name, writer.counts())
        return StorageStatus(ok=True, message="stopped", project_name=name, path=self.path)

    async def add_recording_event(self, req: RecordingEventIn) -> StorageStatus:
        if self.writer is None:
            return StorageStatus(ok=False, message="not recording")
        self._write(self.writer.add_event, req.time if req.time is not None else time.time(),
                    req.kind, req.message, req.detail)
        return StorageStatus(ok=True, message="event added", project_name=self.project_name,
                             path=self.path)

    def readout(self) -> StorageReadout:
        return StorageReadout(
            is_storing=self.writer is not None,
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

    # --- layout and metadata --------------------------------------------------

    async def _ask(self, source: str, op: str, *args):
        """One query to a source for the metadata, or None: a snapshot that cannot be
        had is left out rather than stopping the recording."""
        client = self.sources.get(source)
        if client is None:
            return None
        try:
            return await asyncio.wait_for(getattr(client, op)(*args), SNAPSHOT_TIMEOUT_S)
        except Exception as exc:
            log.info("no %s.%s for the recording's metadata: %s", source, op, exc)
            return None

    async def _build_config(self, req: StorageRequest) -> dict:
        """RecordingWriter's arguments: which streams, their layout, the snapshots."""
        frame_shape = None
        log_schema = None
        detection = None
        meta: dict = {"request": req.model_dump(exclude={"context"})}
        if req.context:
            meta["experiment"] = req.context

        camera = None
        if req.save_frame:
            camera = await self.sources["camera"].get_state()  # frames need it: let it raise
        elif req.save_ai:
            camera = await self._ask("camera", "get_state")  # only sizes the pattern
        if camera is not None:
            meta["camera"] = {"state": _dump(camera),
                              "config": _dump(await self._ask("camera", "get_camera_config"))}
        if req.save_frame:
            frame_shape = camera.frame_dims
            if not frame_shape or len(frame_shape) < 2:
                raise ValueError(f"the camera reports an unusable frame size: {frame_shape}")

        if req.save_log:
            chamber = await self.sources["log"].get_state()
            columns = list(getattr(chamber, "columns", []) or [])
            latest = await self._ask("log", "log")
            sample = latest.entries[-1].values if latest is not None and latest.entries else {}
            if not columns:
                columns = list(sample)
            log_schema = LogSchema.from_sample(columns, sample)
            meta["log"] = {"state": _dump(chamber), "columns": log_schema.columns,
                           "text_columns": sorted(log_schema.text)}

        pld = await self._ask("pld", "chamber_state")
        if pld is not None:
            meta["chamber"] = _dump(pld)

        if req.save_integration:
            meta["integrator"] = {"state": _dump(await self._ask("integrator", "get_state")),
                                  "bboxes": _dump(await self._ask("integrator", "bboxes"))}

        if req.save_ai:
            state = await self.sources["detection"].get_state()
            meta["detection"] = _dump(state)
            dims = (camera.frame_dims if camera is not None and camera.frame_dims else None)
            if not dims and state.crop is not None:
                dims = [state.crop.height, state.crop.width]
            detection = DetectionSchema(
                classes=list(state.classifier_classes or []),
                detector_classes=list(getattr(state, "detector_classes", []) or []),
                pattern_shape=tuple(dims[:2]) if dims else None,
            )

        return {
            "frame_shape": tuple(frame_shape[:2]) if frame_shape else None,
            "log": log_schema,
            "integration": req.save_integration,
            "detection": detection,
            "meta": meta,
            "attrs": {"rheed_energy_kev": float(req.rheed_energy_kev or default_energy_kev())},
        }

    # --- writing -------------------------------------------------------------

    def _write(self, fn, *args, droppable: str | None = None) -> None:
        """Queue one write on the worker. A droppable write (a frame, a detection) is
        dropped and counted when the worker is behind."""
        executor = self._executor
        if executor is None:
            return
        with self._pending_lock:
            if droppable is not None and self._pending >= MAX_PENDING:
                self.dropped[droppable] += 1
                n = self.dropped[droppable]
                if n in (1, 100, 1000) or n % 10000 == 0:
                    log.warning("storage is behind: %d %s dropped so far", n, droppable)
                return
            self._pending += 1

        def run():
            try:
                fn(*args)
            except Exception:
                log.exception("a %s write failed", getattr(fn, "__name__", "storage"))
            finally:
                with self._pending_lock:
                    self._pending -= 1
        try:
            executor.submit(run)
        except RuntimeError:  # shut down while this was on its way in
            with self._pending_lock:
                self._pending -= 1

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_EVERY_S)
            if self.writer is not None:
                self._write(self.writer.flush)

    async def _watch_loop(self, meta: dict) -> None:
        """Turn what changes on the sources into events: chamber events, camera
        settings, the detector's crop, the integration boxes."""
        last = {"camera": (meta.get("camera") or {}).get("config"),
                "crop": (meta.get("detection") or {}).get("crop"),
                "bboxes": (meta.get("integrator") or {}).get("bboxes")}
        since = time.time()
        while True:
            await asyncio.sleep(WATCH_EVERY_S)
            writer = self.writer
            if writer is None:
                return
            events = await self._ask("pld", "recent_events", PldEventQuery(limit=200))
            if events is not None:
                for e in events.events:
                    if e.time > since:
                        self._write(writer.add_event, e.time, f"chamber.{e.kind}", e.message, e.detail)
                if events.events:
                    since = max(since, max(e.time for e in events.events))
            for key, source, op, kind in (("camera", "camera", "get_camera_config", "camera_config"),
                                          ("bboxes", "integrator", "bboxes", "integration_boxes")):
                if key == "camera" and "camera" not in meta:
                    continue
                if key == "bboxes" and "integrator" not in meta:
                    continue
                now = _dump(await self._ask(source, op))
                if now is not None and now != last[key]:
                    self._write(writer.add_event, time.time(), kind, f"{kind} changed",
                                {"before": last[key], "after": now})
                    last[key] = now
            if "detection" in meta:
                state = await self._ask("detection", "get_state")
                crop = _dump(state.crop) if state is not None and state.crop is not None else None
                if state is not None and crop != last["crop"]:
                    self._write(writer.add_event, time.time(), "detection_crop", "detection crop changed",
                                {"before": last["crop"], "after": crop})
                    last["crop"] = crop

    # --- subscriptions ------------------------------------------------------

    async def _subscribe(self, req: StorageRequest) -> None:
        writer = self.writer
        if req.save_frame:
            camera = self.sources["camera"]

            async def on_frame(meta, frame) -> None:
                if self._frame_limit is not None and not self._frame_limit.throttle():
                    return
                self._write(writer.add_frame, frame, meta.time, meta.frame_idx, droppable="frames")

            await camera.on_frame(on_frame)
            await self._ensure_streaming(camera)
            self._subscribed.append(camera)

        if req.save_log:
            chamber_log = self.sources["log"]

            async def on_log(entry, _payload) -> None:
                self._write(writer.add_log, entry.time, entry.values)

            await chamber_log.on_log(on_log)
            await self._ensure_streaming(chamber_log)
            self._subscribed.append(chamber_log)

        if req.save_integration:
            integrator = self.sources["integrator"]

            async def on_integration(sample, _payload) -> None:
                self._write(writer.add_integration, sample.header.time,
                            {k: v.model_dump() for k, v in sample.results.items()})

            await integrator.on_integration(on_integration)
            await self._ensure_streaming(integrator)
            self._subscribed.append(integrator)

        if req.save_ai:
            detection = self.sources["detection"]
            crop = None
            state = await self._ask("detection", "get_state")
            if state is not None and state.crop is not None:
                crop = (state.crop.x, state.crop.y, state.crop.width, state.crop.height)
            self._crop = crop

            async def on_detection(result, arrays) -> None:
                if self._detection_limit is not None and not self._detection_limit.throttle():
                    return
                self._write(writer.add_detection, *_detection_args(result, arrays, self._crop),
                            droppable="detections")

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


def _dump(model):
    """A payload (or None) -> plain JSON-able data for /meta and /events."""
    if model is None:
        return None
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else model


def _detection_args(result, arrays: dict[str, np.ndarray], crop) -> tuple:
    """The detection contract -> RecordingWriter.add_detection's arguments.

    The arrays arrive as a named NPZ archive beside the JSON: the pattern under
    `pattern_ref`, each box's mask under its `mask_ref`.
    """
    boxes = [{"bbox_id": b.bbox_id, "x": b.x, "y": b.y, "width": b.width, "height": b.height,
              "score": b.score, "label": b.label} for b in result.boxes]
    masks = {b.bbox_id: arrays[b.mask_ref] for b in result.boxes if b.mask_ref and b.mask_ref in arrays}
    tracks = {int(k): int(v) for k, v in (result.region2tracks or {}).items()}
    pattern = arrays.get(result.pattern_ref) if result.pattern_ref else None
    return (result.time, result.uuid, boxes, result.classification, pattern, masks, tracks, crop)


class ArchiveHandler:
    """Serves `storage.archive`: finished recordings, read back.

    Every op runs its h5py work in a thread. A frame read is milliseconds, but a
    summary of a long recording or a full integration trace is not, and the node's
    event loop also carries the live recording's traffic.
    """

    def __init__(self, archive: RecordingArchive) -> None:
        self.archive = archive

    async def list_recordings(self, req: ListRecordings) -> RecordingList:
        names = self.archive.names()
        if req.search:
            needle = req.search.lower()
            names = [n for n in names if needle in n.lower()]
        page = names[req.offset:req.offset + req.limit]
        summaries = await asyncio.to_thread(lambda: [self.archive.summary(n) for n in page])
        return RecordingList(recordings=[_summary(RecordingSummary, s) for s in summaries],
                             total=len(names))

    async def recording_info(self, req: RecordingName) -> RecordingInfo:
        return _summary(RecordingInfo, await asyncio.to_thread(self.archive.info, req.name))

    async def recording_frame(self, req: RecordingFrameQuery) -> tuple[RecordingFrameMeta, np.ndarray]:
        meta, image = await asyncio.to_thread(self.archive.frame, req.name, req.index)
        return RecordingFrameMeta(**meta), image

    async def recording_frame_jpeg(self, req: RecordingJpegQuery) -> tuple[RecordingJpegMeta, bytes]:
        def work():
            meta, image = self.archive.frame(req.name, req.index)
            if req.low is None or req.high is None:
                low, high = self.archive.frame_scale(req.name)
            low = req.low if req.low is not None else low
            high = req.high if req.high is not None else high
            if high <= low:
                raise ValueError(f"high ({high}) must be above low ({low})")
            if image.ndim == 3:
                image = image[..., 0]
            scaled = np.clip((image.astype(np.float32) - low) / (high - low), 0.0, 1.0)
            if req.gamma != 1.0:
                scaled **= 1.0 / req.gamma
            pixels = (scaled * 255.0).astype(np.uint8)
            if req.max_width and pixels.shape[1] > req.max_width:
                height = max(1, round(pixels.shape[0] * req.max_width / pixels.shape[1]))
                pixels = cv2.resize(pixels, (req.max_width, height), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", pixels, [int(cv2.IMWRITE_JPEG_QUALITY), req.quality])
            if not ok:
                raise RuntimeError("cv2.imencode rejected the frame")
            return RecordingJpegMeta(
                name=req.name, index=req.index, n_frames=meta["n_frames"],
                time=meta.get("time"), time_stamp=meta.get("time_stamp"),
                width=pixels.shape[1], height=pixels.shape[0], low=low, high=high,
            ), buf.tobytes()
        return await asyncio.to_thread(work)

    async def recording_integration(self, req: RecordingIntegrationQuery) -> RecordingIntegration:
        out = await asyncio.to_thread(self.archive.integration, req.name,
                                      set(req.bbox_ids) if req.bbox_ids is not None else None,
                                      req.since, req.until, req.max_points)
        return RecordingIntegration(**out)

    async def recording_log(self, req: RecordingLogQuery) -> RecordingLog:
        out = await asyncio.to_thread(self.archive.log, req.name, req.columns,
                                      req.since, req.until, req.max_points)
        return RecordingLog(**out)

    async def recording_events(self, req: RecordingEventsQuery) -> RecordingEvents:
        out = await asyncio.to_thread(self.archive.events, req.name,
                                      set(req.kinds) if req.kinds else None,
                                      req.since, req.until, req.limit)
        return RecordingEvents(**out)

    async def recording_detections(self, req: RecordingDetectionsQuery) -> RecordingDetections:
        out = await asyncio.to_thread(self.archive.detections, req.name, req.since, req.until,
                                      req.max_points)
        return RecordingDetections(**out)

    async def recording_detection(self, req: RecordingDetectionQuery) -> RecordingDetection:
        return RecordingDetection(**await asyncio.to_thread(self.archive.detection, req.name, req.index))

    def readout(self) -> ArchiveReadout:
        return ArchiveReadout(root_folder=str(self.archive.root),
                              n_recordings=len(self.archive.names()))


def _summary(model, data: dict):
    """A summary dict -> its payload, with the `_iso` twins of each time filled in."""
    return model(**data, **{f"{k}_iso": iso(data.get(k)) for k in ("modified", "start", "end")
                            if f"{k}_iso" in model.model_fields})
