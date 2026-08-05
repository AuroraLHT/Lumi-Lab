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

from pydantic import BaseModel

from .chamber import LogEntry
from .common import ServerStateBase

# Ack/Empty are reused from .common, not redefined here -- every other contract
# (chamber, storage, rheed) imports the same two models rather than growing its own.


# --- pending confirmation / task tracking -----------------------------------


class PendingConfirmation(BaseModel):
    id: str
    kind: str  # "proceed" | "laser_power" | "mask_center_alignment" | "mask_center_check"
    # | "rheed_gain" | "pixel_check"
    message: str
    requested_at: float


class CurrentTask(BaseModel):
    id: str
    kind: str  # "to_temperature" | "cool_down" | "perform_deposition" | "perform_preablation" | "anneal"
    started_at: float
    detail: dict = {}


class TaskEvent(BaseModel):
    """Pushed on the update channel whenever pending_confirmation or current_task
    changes -- a client that is already connected does not have to poll state in a
    loop to notice a gate opened or a long-running op finished."""

    pending_confirmation: PendingConfirmation | None = None
    current_task: CurrentTask | None = None
    task_result: dict | None = None


class TaskAck(BaseModel):
    """Acknowledges a long-running op was started. Its completion is observed via
    state.current_task clearing, or a TaskEvent with task_result set."""

    ok: bool = True
    task_id: str


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


class RegisterProject(BaseModel):
    project_name: str
    description: str = ""


class ProjectInfo(BaseModel):
    project_id: int
    project_name: str


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


# --- fast hardware control ---------------------------------------------------


class SetTarget(BaseModel):
    target_id: str
    rotation_mode: str = "AUTO"
    twist_mode: str = "AUTO"


class MoveTo(BaseModel):
    position: float


class PixelIndex(BaseModel):
    index: int


class PixelMoveResult(BaseModel):
    index: int
    mask_position: float
    rheed_position: float


class SetMfcFlow(BaseModel):
    mfc_id: str
    flow: float


class StartStorage(BaseModel):
    project_name: str
    is_dryrun: bool = False


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


class ExperimentState(ExperimentReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the driver's readout.
    pass


__all__ = [
    "Anneal",
    "AnnealStep",
    "BeginSetLaserPower",
    "ConfirmCenterMask",
    "ConfirmLaserPower",
    "ConfirmMaskCenter",
    "ConfirmProceed",
    "CoolDown",
    "CurrentSubstrateResponse",
    "CurrentTask",
    "EndStorage",
    "ExperimentReadout",
    "ExperimentRecordId",
    "ExperimentState",
    "FinishCurrentPixel",
    "FinishExperimentRecord",
    "LaserPowerResult",
    "LogEntry",
    "MaskPosition",
    "MfcQuery",
    "MfcStatus",
    "MotorFree",
    "MoveTo",
    "PendingConfirmation",
    "PendingStatus",
    "PerformDeposition",
    "PerformPreablation",
    "PixelCheckStatus",
    "PixelIndex",
    "PixelMoveResult",
    "PixelPosition",
    "PressureReading",
    "ProjectInfo",
    "PumpStatus",
    "RegisterProject",
    "RegisterSubstrate",
    "ResolvePixelCheck",
    "ResumeSubstrate",
    "SetMfcFlow",
    "SetRheedGain",
    "SetTarget",
    "StartStorage",
    "StorageResult",
    "SubstrateInfo",
    "TargetId",
    "TargetMap",
    "TargetName",
    "TaskAck",
    "TaskEvent",
    "TemperatureReading",
    "ToTemperature",
    "ValveStatus",
]
