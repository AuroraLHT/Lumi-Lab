"""The storage node: records the other nodes' streams into HDF5.

Storage is the odd one out -- an "equipment" with no equipment. It is a *consumer*
of RHEED and the chamber, so it can be perfectly healthy and still unable to
record because a source is down. StorageState.deps_available exists to say so out
loud rather than failing opaquely mid-growth.
"""

from __future__ import annotations

from .payloads.common import Empty
from .payloads.storage import (
    ArchiveState,
    ListRecordings,
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
    StorageRequest,
    StorageState,
    StorageStatus,
)
from .spec import Capability, Codec, EquipmentContract, Kind, Op

STORAGE = Capability(
    name="storage",
    kind=Kind.RPC,
    doc="Start and stop an HDF5 recording session.",
    state=StorageState,
    ops=(
        # Named `start_recording`, not `start`: an op called `start` generates a client
        # method that shadows CapabilityClient.start(), so connecting the client would
        # silently fire a request instead. The contract now rejects such names outright.
        Op("start_recording", StorageRequest, StorageStatus),
        Op("stop_recording", Empty, StorageStatus),
    ),
)

ARCHIVE = Capability(
    name="archive",
    kind=Kind.RPC,
    doc="Finished recordings, read back: what is on disk, a frame by index, the RHEED "
        "integration traces and the chamber log each file carries. A recording is named "
        "by its file stem, which is `record_name` in growth.db.",
    state=ArchiveState,
    ops=(
        Op("list_recordings", ListRecordings, RecordingList,
           doc="Recordings on disk, newest name first, with frame/log counts and time span."),
        Op("recording_info", RecordingName, RecordingInfo,
           doc="One recording in full: frame shape, every frame's time (for a scrubber), "
               "the log columns and the integration boxes."),
        Op("recording_frame", RecordingFrameQuery, RecordingFrameMeta, response_codec=Codec.NPY,
           doc="One recorded frame, lossless -- for analysis. A browser wants "
               "recording_frame_jpeg."),
        Op("recording_frame_jpeg", RecordingJpegQuery, RecordingJpegMeta, response_codec=Codec.RAW,
           doc="One recorded frame as a greyscale JPEG, contrast fixed per recording "
               "unless low/high are given. The body is the JFIF buffer."),
        Op("recording_integration", RecordingIntegrationQuery, RecordingIntegration,
           doc="Each integration box's intensity (mean/max/min) over the recording -- the "
               "RHEED oscillation curves."),
        Op("recording_log", RecordingLogQuery, RecordingLog,
           doc="The chamber log rows recorded alongside the frames, as columns."),
    ),
)

STORAGE_NODE = EquipmentContract(
    name="storage",
    exchange="STORAGE",
    version="2.0",
    capabilities=(STORAGE, ARCHIVE),
)
