"""Experiment-driver payloads: PLD deposition sequencing, substrate/pixel
bookkeeping, and the growth database.

Ported from ~/Projects/OperationsNotebooks/src/manager.py (BaseExperimentManager /
SingleDepoExperimentManager / PixelExperimentManager), which drove real growths from
a notebook against a since-retired client API and blocked on real terminal input for
anything a human had to physically do or read. Here that becomes two mechanisms
instead of a monolithic `perform_experiment` RPC:

- Ops that can run long (a temperature ramp, a deposition) return immediately and
  are tracked via `state.current_task`, exactly like CHAMBER.MI_MODE already does
  for a submitted command script -- a handler is one inline `await`, so nothing that
  can take minutes may sit inside an op and still expect its caller's RPC timeout to
  hold.
- Ops that need a human to act or read something physical (laser power meter,
  substrate swap, camera gain, a visual pixel-position check) are split into a
  `begin_*` that returns immediately and sets `state.pending_confirmation`, and a
  `confirm_*` (or the generic `confirm`) that resolves it once the value/decision is
  in. The `id` on PendingConfirmation exists so a stale confirm can't resolve a gate
  that has since moved on.

The canned recipes (`perform_experiment` and friends) are deliberately NOT ops here --
they are plain client-side Python in `lumi.experiment.recipes`, composed from these
primitives, so a notebook user can edit/recombine them freely rather than being stuck
with one fixed server-side sequence.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .chamber import LogEntry
from .common import ServerStateBase

# Request payloads reject unknown fields: a mistyped argument
# (`start_mi_logging(filename=...)` for `file_name`) is silently dropped by
# pydantic's default and the op then runs with the default value and no error.
# Forbidding extras turns that into a ValidationError at the call site.
_STRICT = ConfigDict(extra="forbid")

# Ack/Empty are reused from .common, not redefined here -- every other contract
# (chamber, storage, rheed) imports the same two models rather than growing its own.


# --- pending confirmation / task tracking -----------------------------------


class PendingConfirmation(BaseModel):
    id: str
    kind: str  # "proceed" | "laser_power" | "mask_center_alignment" | "mask_center_check"
    # | "rheed_gain" | "pixel_check" | "fiducial_role"
    message: str
    requested_at: float


class CurrentTask(BaseModel):
    id: str
    kind: str  # "to_temperature" | "cool_down" | "perform_deposition" | "perform_preablation" | "anneal"
    # | "auto_align_center_mask"
    started_at: float
    detail: dict = {}


class TaskEvent(BaseModel):
    """Pushed on the update channel whenever pending_confirmation or current_task
    changes -- a client that is already connected does not have to poll state in a
    loop to notice a gate opened or a long-running op finished.

    `pending_confirmation` and `current_task` are always a full snapshot of both as
    of this event, so None means there is none, not "unchanged". `task_result` is
    set only on the one event of a long-running op finishing, with `finished_task`
    naming the op."""

    pending_confirmation: PendingConfirmation | None = None
    current_task: CurrentTask | None = None
    task_result: dict | None = None
    #: The task `task_result` belongs to, set alongside it -- `current_task` is
    #: already None by then.
    finished_task: CurrentTask | None = None


class TaskAck(BaseModel):
    """Acknowledges a long-running op was started. Its completion is observed via
    state.current_task clearing, or a TaskEvent with task_result set."""

    ok: bool = True
    task_id: str


# --- browsing growth.db: one query shape, one page shape ---------------------
#
# Every list_* request inherits ListQuery and every list_* response carries a
# PageInfo, so a data-management client learns the paging and filtering rules once
# instead of per entity. Timestamps are epoch seconds on the wire, and each one is
# accompanied by an `_iso` twin: both are renderings of a single stored column, so a
# UI gets something sortable and something printable without parsing, and there is
# no second copy to fall out of step.


class ListQuery(BaseModel):
    """Filtering and paging, shared by every list_* op.

    `since`/`until` are epoch seconds and bound the entity's creation time (a step's
    start time). They are converted once, server-side, to whatever the table actually
    stores -- a caller never has to know that `step` holds epoch floats while
    everything else holds UTC text.
    """

    limit: int = 100
    offset: int = 0
    #: Epoch seconds, inclusive. `since=<start of month>` is the "registered this
    #: month" filter; leaving both unset means no time bound at all. Rows written
    #: before their table had a timestamp column match neither bound -- "grown in
    #: September" should not quietly include a growth whose date nobody recorded --
    #: so drop the window to see them.
    since: float | None = None
    until: float | None = None
    #: "desc" (newest first, the default a UI wants) or "asc".
    order: str = "desc"
    #: Case-insensitive substring match across that entity's descriptive columns --
    #: name, uuid, material, and so on. See GrowthDB._TABLES for which.
    search: str | None = None
    #: Retired rows are hidden by default. Nothing is ever deleted, so this is how a
    #: UI offers "show removed".
    include_retired: bool = False


class PageInfo(BaseModel):
    """What a pager needs that the returned rows do not say: how many matched in
    total, and which slice of them this is."""

    total: int = 0
    limit: int = 100
    offset: int = 0
    #: A real field rather than a Python-side property, so the web client sees it too
    #: -- a derived value that only exists on one of two generated clients is a bug
    #: waiting to be found by whichever one goes without.
    has_more: bool = False


# --- substrate / pixel bookkeeping ------------------------------------------


class PixelPosition(BaseModel):
    index: int
    # Offset in mm along the substrate's pixel axis, 0 = substrate center. Converted
    # to a mask/RHEED gun position via PLDChamberConfiguration -- see
    # BaseExperimentManager._pixel_to_mask_position/_pixel_to_rheed_position.
    position: float
    mask_position: float | None = None
    rheed_position: float | None = None
    accessible: bool = True


class SubstrateInfo(BaseModel):
    substrate_id: int
    substrate_uuid: str
    materials: str = ""
    orientation: str = ""
    width: float = 0.0
    height: float = 0.0
    thickness: float = 0.0
    pixel_spacing: float | None = None
    positions: list[PixelPosition] = []
    current_pixel_index: int | None = None
    substrate_name: str | None = None
    manufacturer: str | None = None
    manufacture_date: str | None = None
    #: "active" or "retired". Retired substrates are hidden from list_substrates but
    #: keep every step, sample and measurement recorded against them.
    state: str = "active"
    #: When the substrate was registered. NULL on rows registered before the column
    #: existed -- that reads as "not recorded", never as a fabricated date.
    created_at: float | None = None
    created_at_iso: str | None = None


class SubstrateId(BaseModel):
    substrate_id: int


class RegisterProject(BaseModel):
    project_name: str
    description: str = ""


class ProjectId(BaseModel):
    project_id: int


class ProjectInfo(BaseModel):
    project_id: int
    project_name: str
    description: str | None = None
    created_at: float | None = None
    created_at_iso: str | None = None
    state: str = "active"


class ProjectList(BaseModel):
    projects: list[ProjectInfo] = []
    page: PageInfo = PageInfo()


class UpdateProject(BaseModel):
    project_id: int
    project_name: str | None = None
    description: str | None = None


class RetireProject(BaseModel):
    project_id: int
    retire: bool = True


# --- experiment / record: the growth rows and the files they produced ----------


class ExperimentId(BaseModel):
    experiment_id: int


class ExperimentInfo(BaseModel):
    """One recorded growth. The measured values (`temperature`, `pressure`,
    `laser_power`) are what a person read off an instrument, which is exactly why
    update_experiment exists -- a mistyped pressure is a correction, not a rewrite of
    what happened."""

    experiment_id: int
    experiment_uuid: str | None = None
    substrate_id: int | None = None
    project_id: int | None = None
    is_pixel: bool = False
    pixel_location: int | None = None
    temperature: float | None = None
    pressure: float | None = None
    laser_power: float | None = None
    laser_pulse_rate: float | None = None
    target_material: str | None = None
    num_pulse: int | None = None
    do_preablation: bool = False
    preablation_pulse: int | None = None
    preablation_frequency: int | None = None
    before_experiment_waittime: float | None = None
    after_experiment_waittime: float | None = None
    ramp_rate: float | None = None
    created_at: float | None = None
    created_at_iso: str | None = None
    state: str = "active"


class ExperimentList(BaseModel):
    experiments: list[ExperimentInfo] = []
    page: PageInfo = PageInfo()


class ListExperiments(ListQuery):
    substrate_id: int | None = None
    project_id: int | None = None


class UpdateExperiment(BaseModel):
    experiment_id: int
    project_id: int | None = None
    temperature: float | None = None
    pressure: float | None = None
    laser_power: float | None = None
    laser_pulse_rate: float | None = None
    target_material: str | None = None
    num_pulse: int | None = None
    ramp_rate: float | None = None


class RetireExperiment(BaseModel):
    experiment_id: int
    retire: bool = True


class RecordId(BaseModel):
    record_id: int


class RecordInfo(BaseModel):
    record_id: int
    record_uuid: str | None = None
    experiment_id: int | None = None
    record_name: str | None = None
    created_at: float | None = None
    created_at_iso: str | None = None
    state: str = "active"


class RecordList(BaseModel):
    records: list[RecordInfo] = []
    page: PageInfo = PageInfo()


class ListRecords(ListQuery):
    experiment_id: int | None = None


class UpdateRecord(BaseModel):
    record_id: int
    record_name: str | None = None
    experiment_id: int | None = None


class RetireRecord(BaseModel):
    record_id: int
    retire: bool = True


class RegisterSubstrate(BaseModel):
    materials: str = ""
    orientation: str = ""
    width: float = 0.0
    height: float = 0.0
    thickness: float = 0.0
    substrate_name: str | None = None
    manufacturer: str | None = None
    manufacture_date: str | None = None
    # Even spacing (mm) between pixel positions, generated outward from the
    # substrate center -- mutually exclusive with `positions` (explicit offsets).
    pixel_spacing: float | None = None
    positions: list[float] = []


class ResumeSubstrate(BaseModel):
    substrate_id: int


class CurrentSubstrateResponse(BaseModel):
    substrate: SubstrateInfo | None = None


class UpdateSubstrate(BaseModel):
    """Correct a registered substrate. Every field is optional; only the ones set are
    written, so a request carrying just `width` changes only the width."""

    #: None means the substrate currently loaded on the chamber.
    substrate_id: int | None = None
    materials: str | None = None
    orientation: str | None = None
    thickness: float | None = None
    substrate_name: str | None = None
    manufacturer: str | None = None
    manufacture_date: str | None = None
    # Geometry. These re-derive `positions` and therefore every pixel index, so they
    # are refused once the substrate has a recorded growth -- see the op's doc.
    width: float | None = None
    height: float | None = None
    pixel_spacing: float | None = None
    positions: list[float] | None = None


class ReopenPosition(BaseModel):
    #: None reopens the most recently finished position.
    index: int | None = None
    #: Reopen even though a growth is recorded there. The recorded growth still wins
    #: on the next resume_substrate, which rebuilds progress from the experiment table.
    force: bool = False


class ReopenResult(BaseModel):
    index: int
    substrate: SubstrateInfo | None = None


class ListSubstrates(ListQuery):
    materials: str | None = None


class SubstrateSummary(BaseModel):
    substrate_id: int
    substrate_uuid: str = ""
    substrate_name: str | None = None
    materials: str = ""
    orientation: str = ""
    width: float = 0.0
    pixel_spacing: float | None = None
    num_positions: int = 0
    #: Positions with a recorded growth -- `num_positions - num_used` are left.
    num_used: int = 0
    state: str = "active"
    #: When it was registered. NULL on rows registered before the column existed.
    created_at: float | None = None
    created_at_iso: str | None = None


class SubstrateList(BaseModel):
    substrates: list[SubstrateSummary] = []
    page: PageInfo = PageInfo()


class RetireSubstrate(BaseModel):
    substrate_id: int
    #: False restores a retired substrate.
    retire: bool = True


class TargetMap(BaseModel):
    targets: dict[str, str] = {}


class TargetId(BaseModel):
    target_id: str


class TargetName(BaseModel):
    target_name: str


class FinishCurrentPixel(BaseModel):
    material: str | None = None
    thickness: float | None = None


# --- chamber reads -----------------------------------------------------------


class PressureReading(BaseModel):
    pressure: float
    gauge: str | None = None


class TemperatureReading(BaseModel):
    temperature: float


class ValveStatus(BaseModel):
    valves: dict[str, bool] = {}


class MfcQuery(BaseModel):
    mfc_id: str


class MfcStatus(BaseModel):
    mfc_id: str
    set: float
    monitor: float


class PumpStatus(BaseModel):
    pumps: dict[str, bool] = {}


class MotorFree(BaseModel):
    free: bool


class MaskPosition(BaseModel):
    position: float


class CheckLogging(BaseModel):
    model_config = _STRICT
    # How long to wait for the newest chamber-log row to advance before calling
    # the log stale. Must exceed the controller's `Log Interval` (powers up at
    # 60s; start_mi_logging(1) drops it to 1s).
    timeout_s: float = 5.0


class LoggingAlive(BaseModel):
    alive: bool
    # Seconds waited before the newest row advanced -- the full timeout if it
    # never did.
    waited_s: float
    # The last row timestamp seen, for diagnosing a false "not alive".
    last_stamp: str = ""


# --- fast hardware control ---------------------------------------------------


class SetTarget(BaseModel):
    target_id: str
    rotation_mode: str = "AUTO"
    twist_mode: str = "AUTO"


class MoveTo(BaseModel):
    position: float


class SampleAngle(BaseModel):
    model_config = _STRICT
    # Degrees. For rotate_sample_to this is an absolute stage angle; for
    # rotate_sample_by it is a signed delta from the current angle.
    angle: float


class StartMiLogging(BaseModel):
    model_config = _STRICT
    # Whole seconds between chamber-log rows. The controller powers up at 60; a
    # growth wants 1. An empty file_name lets the driver name the file.
    interval_s: int = 1
    file_name: str = ""


class LoggingStatus(BaseModel):
    file_name: str
    interval_s: int


class PixelIndex(BaseModel):
    index: int


class PixelMoveResult(BaseModel):
    index: int
    mask_position: float
    rheed_position: float


class SetMfcFlow(BaseModel):
    mfc_id: str
    flow: float


class SetMfcControl(BaseModel):
    enabled: bool


class SetPressure(BaseModel):
    pressure: float


class SetPressureControl(BaseModel):
    on: bool


class StartStorage(BaseModel):
    project_name: str
    is_dryrun: bool = False
    # What to record. These map 1:1 onto storage.StorageRequest; the recipe used to
    # hardcode them, which meant a client could not ask for RHEED integrations
    # (save_integration) or overwrite an existing .hdf5 (force_rewrite).
    save_frame: bool = True
    save_ai: bool = True
    save_log: bool = True
    save_integration: bool = True
    force_rewrite: bool = False


class StorageResult(BaseModel):
    ok: bool
    record_uuid: str | None = None
    storage_name: str | None = None
    path: str | None = None
    message: str = ""


class EndStorage(BaseModel):
    is_dryrun: bool = False


class FinishExperimentRecord(BaseModel):
    substrate_id: int
    project_id: int
    experiment_uuid: str
    temperature: float
    pressure: float
    laser_power: float
    laser_repetition_rate: float
    target_material: str
    num_pulse: int
    is_pixel: bool = False
    pixel_index: int | None = None
    storage_name: str | None = None
    record_uuid: str | None = None


class ExperimentRecordId(BaseModel):
    experiment_id: int


# --- long-running automatic ops ----------------------------------------------


class ToTemperature(BaseModel):
    temperature: float
    ramp_rate: float = 20.0


class CoolDown(BaseModel):
    ramp_rate: float = 20.0


class PerformPreablation(BaseModel):
    target_id: str
    num_pulse: int = 1000
    frequency: float = 10.0
    is_dryrun: bool = False
    move_mask_to_block_position: bool = True


class PerformDeposition(BaseModel):
    num_pulse: int
    laser_repetition_rate: float
    target_id: str
    is_dryrun: bool = False


class AnnealStep(BaseModel):
    temperature: float
    ramp_rate: float = 20.0
    wait_time: float = 0.0


class Anneal(BaseModel):
    steps: list[AnnealStep]


# --- gated ops: laser power ---------------------------------------------------


class BeginSetLaserPower(BaseModel):
    laser_power: float
    target_id: str | None = None
    force: bool = False


class ConfirmLaserPower(BaseModel):
    measured_power: float


class LaserPowerResult(BaseModel):
    measured_power: float


# --- gated ops: mask-center calibration / check ------------------------------


class ConfirmCenterMask(BaseModel):
    position: float


class ConfirmMaskCenter(BaseModel):
    aligned: bool
    corrected_position: float | None = None


class CenterMaskPos(BaseModel):
    """A new `center_mask_pos`, mm along Mask1's travel."""

    position: float = Field(ge=0)


class AutoAlignMaskCenter(BaseModel):
    """Scan Mask1 around the current `center_mask_pos` (or `center_mm`) and centre the
    slit on the fiducial marker tagged `mask-center`.

    The first pass scans `center_mm +- half_window_mm` in `step_mm` steps and
    finds the bump in the marker's intensity-vs-position curve. Each later pass
    re-scans just the bump (plus a margin) with `points_per_pass` points, so the step
    shrinks, and re-fits it -- until the centre moves less than `tolerance_mm` between
    passes or `max_passes` is reached. Every pass scans in the same (increasing)
    direction, one steady step after another."""

    #: Where the first scan is centred, mm. None scans around the current
    #: `center_mask_pos`; set it when that calibration is well off (a new mask, a
    #: remount) rather than widening the window.
    center_mm: float | None = Field(default=None, ge=0)
    half_window_mm: float = Field(default=4.0, gt=0, le=30)
    step_mm: float = Field(default=0.5, gt=0, le=5)
    #: Passes in total, the first wide scan included. 1 is the wide scan alone.
    max_passes: int = Field(default=3, ge=1, le=10)
    #: Scan points in each refining pass.
    points_per_pass: int = Field(default=15, ge=5, le=100)
    #: Stop refining once the centre moves less than this between passes.
    tolerance_mm: float = Field(default=0.02, gt=0)
    #: New camera frames averaged per point, after the one current when the move ended.
    frames_per_point: int = Field(default=2, ge=1, le=20)
    #: Least |slit reading - plate reading| accepted as having seen the slit.
    min_contrast: float = Field(default=5.0, gt=0)
    #: False: report where the slit was found, but leave `center_mask_pos` alone.
    apply: bool = True
    #: With no marker tagged `mask-center`, how long to hold a `fiducial_role` pending
    #: confirmation open for someone to tag one before giving up. 0 fails at once.
    role_wait_timeout_s: float = Field(default=600.0, ge=0)


class MaskSweepPoint(BaseModel):
    #: Which scan this point belongs to: 0 the wide one, then each refining pass.
    pass_index: int = 0
    position: float
    reading: float


class MaskAlignPass(BaseModel):
    """One scan's fit."""

    start: float
    stop: float
    step: float
    center: float
    width: float
    contrast: float


class MaskAlignResult(BaseModel):
    """What an `auto_align_center_mask` task reports in its `task_result`."""

    marker_id: str
    #: The `center_mask_pos` this result replaces (or would, with apply=False) -- the
    #: calibration, not where the scan was centred.
    previous_center: float
    center: float
    applied: bool
    #: Whether the last two passes agreed within `tolerance_mm`; False means
    #: `max_passes` ran out first and `center` is the last pass's fit.
    converged: bool
    contrast: float
    baseline: float
    #: +1 the slit reads brighter than the plate, -1 darker.
    polarity: int
    passes: list[MaskAlignPass] = []
    samples: list[MaskSweepPoint] = []


class PendingStatus(BaseModel):
    pending: PendingConfirmation | None = None


# --- gated ops: RHEED gain tuning ---------------------------------------------


class SetRheedGain(BaseModel):
    gain: float


# --- gated ops: per-pixel keep/drop -------------------------------------------


class PixelCheckStatus(BaseModel):
    done: bool = False
    index: int | None = None
    position: float | None = None
    mask_position: float | None = None
    rheed_position: float | None = None
    dropped: list[int] = []


class ResolvePixelCheck(BaseModel):
    keep: bool


# --- generic confirm -----------------------------------------------------------


class ConfirmProceed(BaseModel):
    confirmation_id: str


# --- state ---------------------------------------------------------------------


class ExperimentReadout(BaseModel):
    mode: str = "idle"  # idle | single | pixel
    project_id: int | None = None
    project_name: str | None = None
    substrate_id: int | None = None
    current_pixel_index: int | None = None
    num_pixel_positions: int | None = None
    is_recording: bool = False
    pending_confirmation: PendingConfirmation | None = None
    current_task: CurrentTask | None = None
    # Experiment is a consumer of chamber/rheed/storage, so it can be up but unable
    # to drive anything because a source is down. Surface that rather than failing
    # opaquely mid-growth -- same reasoning as StorageReadout.deps_available.
    deps_available: dict[str, bool] = {}
    laser_power_set: float | None = None
    laser_power_real: float | None = None
    #: Mask1 position, mm, that centres the slit on the sample. Every change is saved
    #: to growth.db and survives a node restart; settings.experiment.pld_config is
    #: only the starting value for a database that has never had one.
    center_mask_pos: float | None = None


class ExperimentState(ExperimentReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the driver's readout.
    pass



# --- sample tracking: what exists, what happened, what it measured ---------------
#
# growth.db lives with the experiment node on the server host, so a notebook on any
# other machine reaches it the same way it reaches the chamber -- through the
# contract. These are the ops that replace the CSV and the pickled `collector` the
# pre-refactor notebooks kept beside the database.


class SampleId(BaseModel):
    sample_id: int


class ListSamples(ListQuery):
    substrate_id: int | None = None
    kind: str | None = None
    #: The growth state -- "planned", "active" or "grown". Not the soft-delete flag;
    #: samples are derived from the substrate's geometry and are never retired.
    state: str | None = None


class SampleInfo(BaseModel):
    sample_id: int
    sample_uuid: str = ""
    parent_sample_id: int | None = None
    substrate_id: int
    kind: str = "position"
    pixel_index: int | None = None
    position_mm: float | None = None
    sample_name: str | None = None
    state: str = "planned"
    notes: str | None = None
    created_at: float | None = None
    created_at_iso: str | None = None


class UpdateSample(BaseModel):
    """Correct a sample's label or growth state. There is no retire here on purpose:
    a sample exists because the substrate's geometry says the position exists, so
    removing one means correcting the geometry (update_substrate) or the growth that
    filled it (retire_experiment)."""

    sample_id: int
    sample_name: str | None = None
    notes: str | None = None
    #: "planned", "active" or "grown".
    state: str | None = None
    position_mm: float | None = None


class SampleList(BaseModel):
    samples: list[SampleInfo] = []
    page: PageInfo = PageInfo()


class LayerInfo(BaseModel):
    """One layer of the stack. Derived from the deposition steps that succeeded on
    this sample, never stored -- see GrowthDB.get_layer_stack."""

    seq: int
    material: str | None = None
    num_pulse: int | None = None
    step_id: int | None = None
    started_at: float | None = None
    started_at_iso: str | None = None
    is_dryrun: bool = False


class SampleDetail(BaseModel):
    sample: SampleInfo | None = None
    layers: list[LayerInfo] = []


class ListSteps(ListQuery):
    sample_id: int | None = None
    session_id: int | None = None
    kind: str | None = None
    #: 500 rather than ListQuery's 100: a single growth runs to several hundred steps
    #: and this default predates the shared shape, so lowering it would silently
    #: truncate history reads that work today.
    limit: int = 500


class StepInfo(BaseModel):
    step_id: int
    parent_step_id: int | None = None
    sample_id: int | None = None
    kind: str
    params: dict = {}
    result: dict = {}
    ok: bool | None = None
    error: str | None = None
    actor: str | None = None
    source: str | None = None
    started_at: float = 0.0
    started_at_iso: str | None = None
    ended_at: float | None = None
    ended_at_iso: str | None = None


class StepList(BaseModel):
    steps: list[StepInfo] = []
    page: PageInfo = PageInfo()


class AddMeasurement(BaseModel):
    sample_id: int
    kind: str
    value: float | None = None
    detail: dict = {}
    source: str | None = None
    step_id: int | None = None
    record_id: int | None = None


class MeasurementId(BaseModel):
    measurement_id: int


class MeasurementInfo(BaseModel):
    measurement_id: int
    sample_id: int
    kind: str
    value: float | None = None
    detail: dict = {}
    source: str | None = None
    #: Epoch seconds. This was a raw SQLite timestamp string before every payload
    #: settled on epoch-plus-ISO; `created_at_iso` is where the string went.
    created_at: float | None = None
    created_at_iso: str | None = None
    state: str = "active"
    #: The growth conditions that produced the sample this measures, resolved
    #: server-side from the linked deposition step. Carried here so assembling a GP
    #: training set is one call rather than one round trip per point.
    conditions: dict = {}


class ListMeasurements(ListQuery):
    sample_id: int | None = None
    kind: str | None = None
    #: Resolved through the sample tree, so this is "every measurement on this
    #: substrate" rather than a plain column match.
    substrate_id: int | None = None


class MeasurementList(BaseModel):
    measurements: list[MeasurementInfo] = []
    page: PageInfo = PageInfo()


class UpdateMeasurement(BaseModel):
    measurement_id: int
    kind: str | None = None
    value: float | None = None
    detail: dict | None = None
    source: str | None = None


class RetireMeasurement(BaseModel):
    measurement_id: int
    retire: bool = True


__all__ = [
    # Re-exported from .chamber: the driver's chamber-read ops answer with the same
    # LogEntry the chamber contract defines rather than a near-copy of it.
    "LogEntry",
    "AddMeasurement",
    "Anneal",
    "AutoAlignMaskCenter",
    "CenterMaskPos",
    "AnnealStep",
    "BeginSetLaserPower",
    "CheckLogging",
    "ConfirmCenterMask",
    "ConfirmLaserPower",
    "ConfirmMaskCenter",
    "ConfirmProceed",
    "CoolDown",
    "CurrentSubstrateResponse",
    "CurrentTask",
    "EndStorage",
    "ExperimentId",
    "ExperimentInfo",
    "ExperimentList",
    "ExperimentReadout",
    "ExperimentRecordId",
    "ExperimentState",
    "FinishCurrentPixel",
    "FinishExperimentRecord",
    "LaserPowerResult",
    "LayerInfo",
    "ListExperiments",
    "ListMeasurements",
    "ListQuery",
    "ListRecords",
    "ListSamples",
    "ListSteps",
    "ListSubstrates",
    "LoggingAlive",
    "LoggingStatus",
    "MaskAlignPass",
    "MaskAlignResult",
    "MaskPosition",
    "MaskSweepPoint",
    "MeasurementId",
    "MeasurementInfo",
    "MeasurementList",
    "MfcQuery",
    "MfcStatus",
    "MotorFree",
    "MoveTo",
    "PageInfo",
    "PendingConfirmation",
    "PendingStatus",
    "PerformDeposition",
    "PerformPreablation",
    "PixelCheckStatus",
    "PixelIndex",
    "PixelMoveResult",
    "PixelPosition",
    "PressureReading",
    "ProjectId",
    "ProjectInfo",
    "ProjectList",
    "PumpStatus",
    "RecordId",
    "RecordInfo",
    "RecordList",
    "RegisterProject",
    "RegisterSubstrate",
    "ReopenPosition",
    "ReopenResult",
    "ResolvePixelCheck",
    "ResumeSubstrate",
    "RetireExperiment",
    "RetireMeasurement",
    "RetireProject",
    "RetireRecord",
    "RetireSubstrate",
    "SampleAngle",
    "SampleDetail",
    "SampleId",
    "SampleInfo",
    "SampleList",
    "SetMfcControl",
    "SetMfcFlow",
    "SetPressure",
    "SetPressureControl",
    "SetRheedGain",
    "SetTarget",
    "StartMiLogging",
    "StartStorage",
    "StepInfo",
    "StepList",
    "StorageResult",
    "SubstrateId",
    "SubstrateInfo",
    "SubstrateList",
    "SubstrateSummary",
    "TargetId",
    "TargetMap",
    "TargetName",
    "TaskAck",
    "TaskEvent",
    "TemperatureReading",
    "ToTemperature",
    "UpdateExperiment",
    "UpdateMeasurement",
    "UpdateProject",
    "UpdateRecord",
    "UpdateSample",
    "UpdateSubstrate",
    "ValveStatus",
]
