"""Storage payloads.

StorageRequest previously existed in three hand-maintained copies that had already
drifted: the pydantic model in api/models.py (no force_rewrite), the
StorageMessageQueueClient.start_storage signature (has it), and the dict the
server destructured -- which had to defensively backfill missing keys. One model now.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .chamber import LogSeries
from .common import ServerStateBase


class StorageRequest(BaseModel):
    project_name: str
    save_frame: bool = False
    save_ai: bool = False
    save_log: bool = False
    save_integration: bool = False
    force_rewrite: bool = False


class StorageStatus(BaseModel):
    ok: bool
    message: str = ""
    project_name: str | None = None
    path: str | None = None


class StorageReadout(BaseModel):
    is_storing: bool = False
    project_name: str | None = None
    path: str | None = None
    # Storage is a consumer of the other nodes, so it can be up but unable to
    # record because a source is down. Surface that rather than failing opaquely.
    deps_available: dict[str, bool] = {}
    # There is deliberately no frame counter here. `n_frames` used to be declared and
    # never assigned, so every heartbeat advertised a number that was always null and
    # every client had to write code for a value that never arrived.
    #
    # A progress count is still wanted, but not on this model: capability state rides
    # the 2s heartbeat, and `NodeRegistry.on_heartbeat` emits `state_changed` whenever
    # the blob differs from the previous one. Every field here is stable for the
    # duration of a recording, so events fire on real transitions only. A value that
    # ticks every 2s would turn the registry into a 0.5 Hz event pump, waking every
    # connected client for the length of a growth. When the counter is built it should
    # ride the getState() control call and be polled by whoever is actually looking.


class StorageState(StorageReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the recorder's readout.
    pass


# --- archive: reading finished recordings back ---------------------------------------
# A recording is named by its file stem, which is exactly `record.record_name` in
# growth.db -- so a history view goes list_records (experiment node) -> the name ->
# these. Every time is epoch seconds with an `_iso` twin, as in the growth.db payloads.


class ListRecordings(BaseModel):
    search: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class RecordingSummary(BaseModel):
    name: str
    size_bytes: int
    modified: float
    modified_iso: str | None = None
    #: The file the storage node is writing right now. Its contents cannot be read back
    #: until the recording stops, so the counts below are absent for it.
    recording: bool = False
    #: Rows actually written. A dataset is pre-allocated in blocks, so its shape
    #: over-counts; these are the recorder's own `size` counters.
    n_frames: int | None = None
    n_log_rows: int | None = None
    n_integrations: int | None = None
    #: First and last frame (or log row, for a recording without frames).
    start: float | None = None
    end: float | None = None
    start_iso: str | None = None
    end_iso: str | None = None
    #: Why the file could not be summarised, if it could not -- a crashed recording
    #: can leave one the HDF5 library refuses to open. Listed rather than hidden.
    error: str | None = None


class RecordingList(BaseModel):
    recordings: list[RecordingSummary] = []
    total: int = 0


class RecordingName(BaseModel):
    name: str


class IntegrationBox(BaseModel):
    bbox_id: int
    center_x: float
    center_y: float
    width: float
    height: float


class RecordingInfo(RecordingSummary):
    frame_shape: list[int] | None = None
    frame_dtype: str | None = None
    #: One entry per frame, so a scrubber can map a time to a frame index.
    frame_times: list[float] = []
    log_columns: list[str] = []
    boxes: list[IntegrationBox] = []
    #: Every dataset in the file -> rows written, for anything not covered above.
    datasets: dict[str, int] = {}


class RecordingFrameQuery(BaseModel):
    name: str
    index: int = Field(ge=0)


class RecordingFrameMeta(BaseModel):
    """Headers for a recorded frame. The body is the np.save buffer (Codec.NPY)."""

    name: str
    index: int
    n_frames: int
    time: float | None = None
    time_stamp: str | None = None
    dtype: str | None = None
    shape: list[int] | None = None


class RecordingJpegQuery(RecordingFrameQuery):
    """A recorded frame as a JPEG, for looking at.

    The frames are 12-bit; a JPEG is 8. `low`/`high` are the raw values mapped to black
    and white. Left out, they are fixed per recording (sampled once across its frames),
    not per frame -- per-frame scaling makes the pattern flicker while scrubbing and
    hides exactly the intensity changes a growth is judged by.
    """

    low: float | None = None
    high: float | None = None
    gamma: float = Field(default=1.0, gt=0)
    #: Downscale to at most this width, e.g. for a thumbnail strip.
    max_width: int | None = Field(default=None, ge=16)
    quality: int = Field(default=85, ge=10, le=100)


class RecordingJpegMeta(BaseModel):
    """Headers for a recorded frame as JPEG. The body is the JFIF buffer (Codec.RAW)."""

    name: str
    index: int
    n_frames: int
    time: float | None = None
    time_stamp: str | None = None
    width: int
    height: int
    #: The mapping actually applied, so a UI can show it and hold it steady.
    low: float
    high: float


class RecordingIntegrationQuery(BaseModel):
    name: str
    #: None means every box.
    bbox_ids: list[int] | None = None
    since: float | None = None
    until: float | None = None
    max_points: int = Field(default=4000, ge=2, le=200_000)


class IntegrationTrace(IntegrationBox):
    """One box's intensity over the recording -- where the RHEED oscillations are."""

    time: list[float] = []
    mean: list[float] = []
    max: list[float] = []
    min: list[float] = []
    n_points: int = 0
    stride: int = 1


class RecordingIntegration(BaseModel):
    name: str
    traces: list[IntegrationTrace] = []


class RecordingLogQuery(BaseModel):
    name: str
    #: Log column names; None means lumi.contracts.payloads.chamber.DEFAULT_LOG_COLUMNS.
    columns: list[str] | None = None
    since: float | None = None
    until: float | None = None
    max_points: int = Field(default=2000, ge=2, le=100_000)


class RecordingLog(LogSeries):
    name: str


class ArchiveReadout(BaseModel):
    root_folder: str | None = None
    n_recordings: int | None = None


class ArchiveState(ArchiveReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the archive's readout.
    pass
