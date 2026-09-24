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
    AutoAlignMaskCenter,
    BeginSetLaserPower,
    CheckLogging,
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
    LoggingAlive,
    LoggingStatus,
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
    ExperimentId,
    ExperimentInfo,
    ExperimentList,
    ListExperiments,
    ListQuery,
    ListRecords,
    ProjectId,
    ProjectList,
    RecordId,
    RecordInfo,
    RecordList,
    RegisterProject,
    RegisterSubstrate,
    ReopenPosition,
    ReopenResult,
    ResolvePixelCheck,
    ResumeSubstrate,
    RetireExperiment,
    RetireMeasurement,
    RetireProject,
    RetireRecord,
    RetireSubstrate,
    SubstrateId,
    UpdateExperiment,
    UpdateMeasurement,
    UpdateProject,
    UpdateRecord,
    UpdateSample,
    SampleAngle,
    SampleDetail,
    SampleId,
    SampleInfo,
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
    StartMiLogging,
    StartStorage,
    StorageResult,
    SubstrateInfo,
    SubstrateList,
    SubstrateSummary,
    ListSubstrates,
    UpdateSubstrate,
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

        # --- bookkeeping corrections. Registration is otherwise write-once, which
        # made a mistyped size or a mis-clicked finish unfixable except by
        # registering a second, fictional substrate.
        Op("list_substrates", ListSubstrates, SubstrateList,
           doc="Every registered substrate: what it is, how many positions it has "
               "and how many are already grown on. resume_substrate takes an id and "
               "this is where you find one. Takes the shared list query -- limit, "
               "offset, since/until (epoch seconds), order, search, include_retired -- "
               "so 'the last 5' is limit=5 and 'registered this month' is since=<start "
               "of month>. Retired substrates are hidden unless include_retired."),
        Op("get_substrate", SubstrateId, SubstrateInfo,
           doc="One substrate by id, with its positions and which are still "
               "accessible. current_substrate answers only for the one on the "
               "chamber; this reads any of them."),
        Op("update_substrate", UpdateSubstrate, SubstrateInfo,
           doc="Correct a registered substrate's record; only the fields you set are "
               "written. substrate_id defaults to the one loaded on the chamber. "
               "Descriptive fields (materials, orientation, thickness, name, "
               "manufacturer, manufacture_date) are always editable. Geometry (width, "
               "height, pixel_spacing, positions) re-derives every pixel index, so it "
               "is refused once the substrate has a recorded growth -- before then it "
               "rebuilds the positions and their sample rows, exactly as registering "
               "with the corrected value would have.",
           journal=True),
        Op("reopen_position", ReopenPosition, ReopenResult,
           doc="Undo a finish_substrate/finish_current_pixel that was not meant: puts "
               "the position back in play and its sample back to 'planned'. Defaults "
               "to the most recently finished position. Refuses a position that has a "
               "recorded growth unless force -- and even forced, resume_substrate "
               "rebuilds progress from the experiment table and will mark it spent "
               "again.",
           journal=True),
        Op("retire_substrate", RetireSubstrate, SubstrateInfo,
           doc="Hide a substrate from list_substrates without deleting it -- for one "
               "registered by mistake. Nothing recorded against it is destroyed, so "
               "its steps and samples stay readable. retire=false restores it.",
           journal=True),
        Op("unload_substrate", Empty, CurrentSubstrateResponse,
           doc="Take the current substrate off the chamber without touching the "
               "database, for when the wrong one was registered or resumed. Returns "
               "whatever is current afterwards, which is usually nothing."),

        # --- growth.db as data, for a management UI. Every list_* takes the same
        # query (limit/offset/since/until/order/search/include_retired) and answers
        # with the same PageInfo; every update_* writes only the fields it is given;
        # every retire_* is a soft delete, because growth.db is the only record of
        # what this chamber has ever grown and has no restore path. `step` has no
        # editor at all -- the journal is append-only so that what happened cannot be
        # rewritten after the fact.
        Op("list_projects", ListQuery, ProjectList,
           doc="Projects, newest first. Same query shape as every other list_*."),
        Op("get_project", ProjectId, ProjectInfo),
        Op("update_project", UpdateProject, ProjectInfo,
           doc="Rename a project or fix its description. Only the fields you set are "
               "written.", journal=True),
        Op("retire_project", RetireProject, ProjectInfo,
           doc="Hide a project from listings. Its experiments are untouched and stay "
               "readable; retire=false restores it.", journal=True),

        Op("list_experiments", ListExperiments, ExperimentList,
           doc="Recorded growths, newest first, optionally narrowed to one substrate "
               "or project and to a time window."),
        Op("get_experiment", ExperimentId, ExperimentInfo),
        Op("update_experiment", UpdateExperiment, ExperimentInfo,
           doc="Correct a growth's recorded conditions -- the temperature, pressure "
               "and laser power a person read off an instrument and may have typed "
               "wrong. Identity (uuid, substrate, pixel) is not editable: that would "
               "make it a different growth.", journal=True),
        Op("retire_experiment", RetireExperiment, ExperimentInfo,
           doc="Mark a growth as recorded by mistake -- a dry run logged as real, a "
               "duplicate row. It stops counting as a growth, which also hands its "
               "pixel position back, so a substrate wrongly marked spent becomes "
               "usable again. Nothing is deleted; retire=false restores it.",
           journal=True),

        Op("list_records", ListRecords, RecordList,
           doc="Storage recordings (the .hdf5 files), optionally for one experiment."),
        Op("get_record", RecordId, RecordInfo),
        Op("update_record", UpdateRecord, RecordInfo,
           doc="Rename a recording or re-link it to the right experiment -- the fix "
               "for a growth whose file was attached to the wrong row.", journal=True),
        Op("retire_record", RetireRecord, RecordInfo, journal=True),

        Op("update_sample", UpdateSample, SampleInfo,
           doc="Correct a sample's name, notes, growth state or position. There is no "
               "retire: a sample exists because the substrate geometry says its "
               "position exists, so removing one means update_substrate (fix the "
               "geometry) or retire_experiment (undo the growth that filled it).",
           journal=True),

        Op("get_measurement", MeasurementId, MeasurementInfo),
        Op("update_measurement", UpdateMeasurement, MeasurementInfo,
           doc="Correct a measurement's value, kind, detail or source -- an ex-situ "
               "result entered before the analysis was final.", journal=True),
        Op("retire_measurement", RetireMeasurement, MeasurementInfo,
           doc="Hide a measurement from listings, so a wrong value stops reaching an "
               "optimiser without vanishing from the record.", journal=True),

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
        Op("check_logging_alive", CheckLogging, LoggingAlive,
           doc="Probe whether the chamber log is still being written: read the "
               "newest row's timestamp, wait up to timeout_s, and report whether "
               "it advanced (with last_stamp, the timestamp seen). A frozen "
               "timestamp means the log-reader thread died or PASCAL stopped "
               "writing -- every downstream read is then stale. timeout_s must "
               "exceed the controller's Log Interval (60s at power-up; "
               "start_mi_logging(interval_s=1) drops it to 1s)."),

        # --- fast hardware control
        Op("set_target", SetTarget, Ack, journal=True),
        Op("start_mi_logging", StartMiLogging, LoggingStatus,
           doc="Start PASCAL data logging at a fixed whole-second row interval "
               "(`Log Interval` + `Data Logging File=`). The controller powers up "
               "at 60s; pass interval_s=1 for a growth. Empty file_name lets the "
               "driver name the file. Returns the file name and interval in use. "
               "PASCAL will not switch files while a logger is already open -- call "
               "stop_mi_logging first if one might be running.",
           journal=True),
        Op("stop_mi_logging", Empty, Ack,
           doc="Close PASCAL's currently open data-logging file (`Data Logging "
               "OFF`). Needed before start_mi_logging can point at a new file: an "
               "already-running logger makes start_mi_logging ack without switching.",
           journal=True),
        Op("move_mask_to_position", MoveTo, Ack, journal=True),
        Op("move_rheed_to_position", MoveTo, Ack, journal=True),
        Op("rotate_sample_to", SampleAngle, Ack,
           doc="Rotate the sample stage to an absolute angle in degrees (PASCAL "
               "`Set Sample Position`). Blocks until the move completes. Refused "
               "while the motor holding lock is released (see is_motor_free).",
           journal=True),
        Op("rotate_sample_by", SampleAngle, Ack,
           doc="Rotate the sample stage by a signed delta in degrees (PASCAL "
               "`Rotate Sample`); negative turns the other way. Blocks until the "
               "move completes. Refused while the motor holding lock is released.",
           journal=True),
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
               "immediately rather than setting a setpoint no current can reach. "
               "A setpoint below the PID-engage threshold is not holdable: from a hot "
               "chamber it is commanded as a cooldown (no PID), and from an already-"
               "cold one it is a no-op, since a room-temperature growth runs with the "
               "diode off.", journal=True),
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
        # --- automatic: the same calibration, read off the chamber camera
        Op("auto_align_center_mask", AutoAlignMaskCenter, TaskAck,
           doc="Scan Mask1 around center_mask_pos, watching the fiducial marker tagged "
               "'mask-center' (the sample's centre), locate the slit from the "
               "intensity-vs-position curve, then re-scan just the slit, finer each pass, "
               "until the centre settles. Sets center_mask_pos to it and leaves the mask "
               "there. Long-running: the MaskAlignResult (centre, each pass's fit, every "
               "scanned point) arrives as the task_result. If no marker is tagged "
               "'mask-center' it first opens a 'fiducial_role' pending confirmation and "
               "waits for one to be tagged (chamber.fiducial.set_role), then continues; "
               "`confirm` on that confirmation cancels the alignment.", journal=True),

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
