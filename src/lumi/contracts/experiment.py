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
    SampleDetail,
    SampleId,
    SampleList,
    ListSamples,
    ListSteps,
    StepList,
    AddMeasurement,
    MeasurementId,
    MeasurementInfo,
    MeasurementList,
    ListMeasurements,
    SetMfcControl,
    SetMfcFlow,
    SetPressure,
    SetPressureControl,
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
        Op("register_project", RegisterProject, ProjectInfo, journal=True),
        Op("register_substrate", RegisterSubstrate, SubstrateInfo, journal=True),
        Op("resume_substrate", ResumeSubstrate, SubstrateInfo, journal=True),
        Op("show_available_targets", Empty, TargetMap),
        Op("get_target_name_by_id", TargetId, TargetName),
        Op("get_target_id_by_name", TargetName, TargetId),
        Op("current_substrate", Empty, CurrentSubstrateResponse),
        Op("finish_substrate", Empty, Ack, journal=True),
        Op("finish_current_pixel", FinishCurrentPixel, Ack, journal=True),

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
        Op("set_target", SetTarget, Ack, journal=True),
        Op("move_mask_to_position", MoveTo, Ack, journal=True),
        Op("move_rheed_to_position", MoveTo, Ack, journal=True),
        Op("to_pixel", PixelIndex, PixelMoveResult, journal=True),
        Op("to_current_pixel", Empty, PixelMoveResult, journal=True),
        # --- gas. The setpoint ops and the gates are deliberately separate, because
        # `MFC Control` is a single master enable with no channel argument: folding it
        # into set_mfc_flow would mean set_mfc_flow(2, 0) silently shuts MFC1 too.
        # Nothing in the chamber log reports either gate back (there is no bit for
        # them in PARSE_DICT), so the requirement is stated here, in the tool
        # descriptions an agent actually reads, rather than left to be discovered.
        Op("set_mfc_flow", SetMfcFlow, Ack,
           doc="Set an MFC's flow setpoint in sccm. The gas does NOT flow until "
               "set_mfc_control(enabled=True) opens the master gate -- until then "
               "get_mfc_status shows `set` at your value and `monitor` at zero, and "
               "the chamber pressure does not move.", journal=True),
        Op("set_mfc_control", SetMfcControl, Ack,
           doc="Open or close the MFC master gate (PASCAL's `MFC Control`). One gate "
               "for every channel, so disabling it stops all MFCs at once, leaving "
               "their flow setpoints untouched. Not needed under pressure control, "
               "which drives the control MFC itself.", journal=True),
        Op("set_pressure", SetPressure, Ack,
           doc="Set the closed-loop pressure setpoint in Torr. Takes effect only "
               "once set_pressure_control(on=True) is on; the controller then trims "
               "the control MFC's flow to hold it, overriding set_mfc_flow on that "
               "channel.", journal=True),
        Op("set_pressure_control", SetPressureControl, Ack,
           doc="Turn closed-loop pressure control on or off. On: the controller owns "
               "the control MFC and holds set_pressure's setpoint. Off: flow reverts "
               "to whatever set_mfc_flow last set, gated by set_mfc_control.", journal=True),
        Op("initiate_heating_laser", Empty, Ack, journal=True),
        Op("turn_off_heating_laser", Empty, Ack, journal=True),
        Op("start_storage", StartStorage, StorageResult, journal=True),
        Op("end_storage", EndStorage, StorageResult, journal=True),
        Op("finish_experiment_record", FinishExperimentRecord, ExperimentRecordId, journal=True),

        # --- long-running automatic ops: return immediately, tracked via
        # state.current_task (see TaskEvent on the update channel).
        Op("to_temperature", ToTemperature, TaskAck,
           doc="Ramp to a temperature and engage PID. Requires the heating laser to "
               "be on already -- call initiate_heating_laser first, or this fails "
               "immediately rather than setting a setpoint no current can reach.", journal=True),
        Op("cool_down", CoolDown, TaskAck, journal=True),
        Op("perform_preablation", PerformPreablation, TaskAck, journal=True),
        Op("perform_deposition", PerformDeposition, TaskAck, journal=True),
        Op("anneal", Anneal, TaskAck, journal=True),

        # --- gated: laser power (unread physical meter)
        Op("begin_set_laser_power", BeginSetLaserPower, Ack, journal=True),
        Op("confirm_laser_power", ConfirmLaserPower, LaserPowerResult, journal=True),

        # --- gated: mask-center calibration / check (visual judgement)
        Op("begin_align_center_mask", Empty, Ack, journal=True),
        Op("confirm_center_mask", ConfirmCenterMask, Ack, journal=True),
        Op("begin_check_mask_center", Empty, Ack, journal=True),
        Op("confirm_mask_center", ConfirmMaskCenter, PendingStatus, journal=True),

        # --- gated: RHEED gain tuning (live view, iterative)
        Op("begin_adjust_rheed_gain", Empty, Ack, journal=True),
        Op("set_rheed_gain", SetRheedGain, Ack, journal=True),
        Op("confirm_rheed_gain", Empty, Ack, journal=True),

        # --- gated: per-pixel keep/drop (visual judgement per position)
        Op("begin_check_rheed_pixels", Empty, PixelCheckStatus, journal=True),
        Op("resolve_pixel_check", ResolvePixelCheck, PixelCheckStatus, journal=True),

        # --- sample tracking. Reads of what the journal and the sample tree already
        # hold, plus the one write a client needs: attaching a measurement. growth.db
        # is on this node's host, so these are how a notebook elsewhere reaches it.
        Op("list_samples", ListSamples, SampleList,
           doc="Every sample on a substrate (or all of them), with its position and "
               "state. A sample exists per growable position from registration, so "
               "this answers 'which positions are left' without any in-memory state."),
        Op("get_sample", SampleId, SampleDetail,
           doc="One sample plus its layer stack, bottom-up. The stack is derived from "
               "the deposition steps that succeeded on it, so it reflects what was "
               "actually grown rather than what was planned."),
        Op("sample_history", ListSteps, StepList,
           doc="The step journal: what happened, in order, with parameters, outcome, "
               "duration and who asked for it. Filter by sample or by chamber session."),
        Op("add_measurement", AddMeasurement, MeasurementId, journal=True,
           doc="Attach a result to a sample -- a RHEED growth metric, or an ex-situ "
               "XRD/AFM/PFM/transport measurement. `value` is the scalar an optimiser "
               "sorts on; `detail` carries the full result."),
        Op("list_measurements", ListMeasurements, MeasurementList,
           doc="Measurements, with the growth conditions that produced each sample "
               "resolved alongside them -- one call for a GP training set."),

        # --- generic "just confirm, no captured value" gate -- everything else
        # that was an ainput with no return value (flip the laser to ON/Standby,
        # "substrate replaced, press enter", etc).
        Op("confirm", ConfirmProceed, Ack, journal=True),
    ),
    update=StreamSpec("pending", TaskEvent),
)

EXPERIMENT = EquipmentContract(
    name="experiment",
    exchange="EXPERIMENT",
    version="1.0",
    capabilities=(DRIVER,),
)
