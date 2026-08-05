"""The experiment node: drives a PLD growth end to end.

One capability, `driver`, not a split between bookkeeping and hardware control --
they share the same mutable state (current substrate, current pixel) and there is
exactly one physical chamber in flight at a time, so a split would only duplicate
that state across two handlers for no isolation benefit.

PUBSUB, not RPC, for the same reason CHAMBER.MI_MODE is PUBSUB: some ops
(temperature ramps, depositions) can run for a long time, and some (anything that
needs a human to act or read something physical) can't resolve within a single
request at all. Both return immediately and are followed up via `state.current_task`
/ `state.pending_confirmation`, with `driver.update` broadcasting the transition the
moment it happens so a connected client does not have to poll in a loop.

See `lumi.experiment` for the ported driving logic and `lumi.experiment.recipes` for
the canned sequences (ramp -> preablate -> deposit -> record) -- deliberately client-
side Python, not ops here, so a notebook user can recombine the primitives below
freely instead of being stuck with one fixed server-side recipe.
"""

from __future__ import annotations

from .payloads.common import Ack, Empty
from .payloads.experiment import (
    Anneal,
    BeginSetLaserPower,
    ConfirmCenterMask,
    ConfirmLaserPower,
    ConfirmMaskCenter,
    ConfirmProceed,
    CoolDown,
    CurrentSubstrateResponse,
    EndStorage,
    ExperimentRecordId,
    ExperimentState,
    FinishCurrentPixel,
    FinishExperimentRecord,
    LaserPowerResult,
    LogEntry,
    MaskPosition,
    MfcQuery,
    MfcStatus,
    MotorFree,
    MoveTo,
    PendingStatus,
    PerformDeposition,
    PerformPreablation,
    PixelCheckStatus,
    PixelIndex,
    PixelMoveResult,
    PressureReading,
    ProjectInfo,
    PumpStatus,
    RegisterProject,
    RegisterSubstrate,
    ResolvePixelCheck,
    ResumeSubstrate,
    SetMfcFlow,
    SetRheedGain,
    SetTarget,
    StartStorage,
    StorageResult,
    SubstrateInfo,
    TargetId,
    TargetMap,
    TargetName,
    TaskAck,
    TaskEvent,
    TemperatureReading,
    ToTemperature,
    ValveStatus,
)
from .spec import Capability, EquipmentContract, Kind, Op, StreamSpec

DRIVER = Capability(
    name="driver",
    kind=Kind.PUBSUB,
    doc="Drive a PLD growth: substrate/pixel bookkeeping, chamber reads, hardware "
        "control, and the human-gated/long-running steps a real growth needs.",
    state=ExperimentState,
    ops=(
        # --- bookkeeping: the old `manual_input=True` prompts are just typed
        # request fields now -- no gating, the caller already has the value.
        Op("register_project", RegisterProject, ProjectInfo),
        Op("register_substrate", RegisterSubstrate, SubstrateInfo),
        Op("resume_substrate", ResumeSubstrate, SubstrateInfo),
        Op("show_available_targets", Empty, TargetMap),
        Op("get_target_name_by_id", TargetId, TargetName),
        Op("get_target_id_by_name", TargetName, TargetId),
        Op("current_substrate", Empty, CurrentSubstrateResponse),
        Op("finish_substrate", Empty, Ack),
        Op("finish_current_pixel", FinishCurrentPixel, Ack),

        # --- chamber reads: domain interpretation of the raw log row (gauge
        # fallback, field-name mapping) that today only exists in manager.py.
        Op("get_current_log", Empty, LogEntry),
        Op("get_current_pressure", Empty, PressureReading),
        Op("get_current_temperature", Empty, TemperatureReading),
        Op("get_valve_status", Empty, ValveStatus),
        Op("get_mfc_status", MfcQuery, MfcStatus),
        Op("get_pump_status", Empty, PumpStatus),
        Op("is_motor_free", Empty, MotorFree),
        Op("get_current_mask_position", Empty, MaskPosition),

        # --- fast hardware control
        Op("set_target", SetTarget, Ack),
        Op("move_mask_to_position", MoveTo, Ack),
        Op("move_rheed_to_position", MoveTo, Ack),
        Op("to_pixel", PixelIndex, PixelMoveResult),
        Op("to_current_pixel", Empty, PixelMoveResult),
        Op("set_mfc_flow", SetMfcFlow, Ack),
        Op("initiate_heating_laser", Empty, Ack),
        Op("turn_off_heating_laser", Empty, Ack),
        Op("start_storage", StartStorage, StorageResult),
        Op("end_storage", EndStorage, StorageResult),
        Op("finish_experiment_record", FinishExperimentRecord, ExperimentRecordId),

        # --- long-running automatic ops: return immediately, tracked via
        # state.current_task (see TaskEvent on the update channel).
        Op("to_temperature", ToTemperature, TaskAck),
        Op("cool_down", CoolDown, TaskAck),
        Op("perform_preablation", PerformPreablation, TaskAck),
        Op("perform_deposition", PerformDeposition, TaskAck),
        Op("anneal", Anneal, TaskAck),

        # --- gated: laser power (unread physical meter)
        Op("begin_set_laser_power", BeginSetLaserPower, Ack),
        Op("confirm_laser_power", ConfirmLaserPower, LaserPowerResult),

        # --- gated: mask-center calibration / check (visual judgement)
        Op("begin_align_center_mask", Empty, Ack),
        Op("confirm_center_mask", ConfirmCenterMask, Ack),
        Op("begin_check_mask_center", Empty, Ack),
        Op("confirm_mask_center", ConfirmMaskCenter, PendingStatus),

        # --- gated: RHEED gain tuning (live view, iterative)
        Op("begin_adjust_rheed_gain", Empty, Ack),
        Op("set_rheed_gain", SetRheedGain, Ack),
        Op("confirm_rheed_gain", Empty, Ack),

        # --- gated: per-pixel keep/drop (visual judgement per position)
        Op("begin_check_rheed_pixels", Empty, PixelCheckStatus),
        Op("resolve_pixel_check", ResolvePixelCheck, PixelCheckStatus),

        # --- generic "just confirm, no captured value" gate -- everything else
        # that was an ainput with no return value (flip the laser to ON/Standby,
        # "substrate replaced, press enter", etc).
        Op("confirm", ConfirmProceed, Ack),
    ),
    update=StreamSpec("pending", TaskEvent),
)

EXPERIMENT = EquipmentContract(
    name="experiment",
    exchange="EXPERIMENT",
    version="1.0",
    capabilities=(DRIVER,),
)
