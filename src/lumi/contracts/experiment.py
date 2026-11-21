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
    FinishRheedOn,
    RheedGunResult,
    RheedOn,
    ConfirmLaserTest,
    LaserTest,
    LaserTestList,
    ListLaserTests,
    ListSequences,
    GetSequence,
    SaveSequence,
    SequenceInfo,
    SequenceList,
    RunSequence,
    SequenceRef,
    SetTargetPreablation,
    TargetPreablation,
    TargetPreablationList,
    AddMeasurement,
    Anneal,
    AttachMeasurementFile,
    AutoAlignMaskCenter,
    BeginSetLaserPower,
    CenterMaskPos,
    CheckLogging,
    CheckMiMode,
    ConfirmCenterMask,
    ConfirmLaserPower,
    ConfirmMaskCenter,
    ConfirmProceed,
    CoolDown,
    CurrentSubstrateResponse,
    CurrentTarget,
    EndStorage,
    ExperimentId,
    ExperimentInfo,
    ExperimentList,
    ExperimentRecordId,
    ExperimentState,
    FinishCurrentPixel,
    FinishExperimentRecord,
    LaserPowerResult,
    LaserSettings,
    ListExperiments,
    ListMeasurements,
    ListQuery,
    ListRecords,
    ListSamples,
    ListSnapshots,
    ListSteps,
    ListSubstrates,
    LogEntry,
    LoggingAlive,
    LoggingStatus,
    MaskAlignResult,
    MaskPosition,
    MeasurementFileId,
    MeasurementFileInfo,
    MeasurementId,
    MeasurementInfo,
    MeasurementList,
    MfcQuery,
    MiModeStatus,
    MfcStatus,
    MotorFree,
    MoveMask,
    MoveTo,
    PendingStatus,
    PerformDeposition,
    PerformPreablation,
    PixelCheckStatus,
    PixelIndex,
    PixelMoveResult,
    PressureReading,
    ProjectId,
    ProjectInfo,
    ProjectList,
    PumpStatus,
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
    RetireMeasurementFile,
    RetireProject,
    RetireRecord,
    RetireSnapshot,
    RetireSubstrate,
    SampleAngle,
    SampleDetail,
    SampleId,
    SampleInfo,
    SampleList,
    SetLaserEnergy,
    SetLaserHv,
    SetMfcControl,
    SetMfcFlow,
    SetPressure,
    SetPressureControl,
    SetRheedGain,
    SetTarget,
    SnapshotId,
    SnapshotInfo,
    SnapshotList,
    StartMiLogging,
    StartStorage,
    StepList,
    CancelTask,
    CancelTaskResult,
    StopDeposition,
    StopDepositionResult,
    StorageResult,
    SubstrateId,
    SubstrateInfo,
    SubstrateList,
    SubstrateSummary,
    TakeSnapshot,
    TargetId,
    TargetMap,
    TargetName,
    TaskAck,
    TaskEvent,
    TemperatureReading,
    ToTemperature,
    TurnOffHeatingLaser,
    UpdateExperiment,
    UpdateMeasurement,
    UpdateProject,
    UpdateRecord,
    UpdateSample,
    UpdateSubstrate,
    ValveStatus,
)
from .spec import Capability, Codec, EquipmentContract, Kind, Op, StreamSpec

#: How long a caller waits on an op that blocks on a motor move: up to
#: `experiment.bounds.motor_ready_timeout` (60 s) for the holding lock, then the move
#: itself -- a carousel revolve or a sample rotation at 10 deg/s takes up to ~36 s, and
#: confirm_mask_center makes two moves.
MOTION_TIMEOUT_S = 120.0

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
        Op("get_current_target", Empty, CurrentTarget,
           doc="Which carousel slot is in front of the laser now, read from the chamber "
               "log's `TG No.`. The log counts from 0: 0-5 are targets A-F, 6 is Clear, "
               "7 is Monitor. The chamber camera only sees the sample with Clear or "
               "Monitor in place; at any other slot the target is in the way."),
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
               "and laser pulse energy a person read off an instrument and may have typed "
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
        Op("attach_measurement_file", AttachMeasurementFile, MeasurementFileInfo,
           request_codec=Codec.RAW, journal=True,
           doc="Attach a file to a measurement: the instrument's raw file, an AFM/PFM "
               "image or map, a micrograph. The request body is the file itself. Kept "
               "byte for byte, whether or not anything can parse it yet."),
        Op("measurement_file", MeasurementFileId, MeasurementFileInfo, response_codec=Codec.RAW,
           doc="An attached file's bytes, exactly as uploaded."),
        Op("retire_measurement_file", RetireMeasurementFile, MeasurementFileInfo, journal=True,
           doc="Hide a mistaken upload. The bytes stay on disk."),

        # --- snapshots: a still from a camera, kept with the growth it belongs to.
        Op("take_snapshot", TakeSnapshot, SnapshotInfo, journal=True,
           doc="Save the current frame of the chamber or RHEED camera, labelled with the "
               "growth stage, against the loaded sample and this chamber session. Kept "
               "twice: losslessly (snapshot_frame) and as a JPEG to look at (snapshot_jpeg)."),
        Op("list_snapshots", ListSnapshots, SnapshotList,
           doc="Snapshots, oldest first, narrowed by sample, session, camera or stage."),
        Op("snapshot_jpeg", SnapshotId, SnapshotInfo, response_codec=Codec.RAW,
           doc="A snapshot as a JPEG, to look at."),
        Op("snapshot_frame", SnapshotId, SnapshotInfo, response_codec=Codec.NPY,
           doc="A snapshot's frame exactly as the camera gave it, for analysis."),
        Op("retire_snapshot", RetireSnapshot, SnapshotInfo, journal=True,
           doc="Hide a mistaken snapshot from listings. The files stay on disk."),

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
               "timestamp means the log-reader thread died or the log stopped being "
               "written (PASCAL, or the chamber node under pld-service) -- storage's "
               "chamber log is then stale. Under pld-service the experiment driver's "
               "own readings come from the service's live state, not this log. "
               "timeout_s must exceed the Log Interval (60s at PASCAL power-up; "
               "start_mi_logging(interval_s=1) drops it to 1s)."),

        Op("check_mi_mode", CheckMiMode, MiModeStatus,
           doc="Check that the chamber is taking commands from this software. Run it "
               "before a growth. With the pld-service driver (the default): `on` when "
               "the service is connected and operable, `off` when it is not answering "
               "or is read-only on hardware (a person must restart it with "
               "--allow-operations), `busy` when another client (the REA GUI) holds "
               "control. With --use-legacy-mi-mode: sends a no-op MI script (`Wait 0 "
               "sec`) and reports `on` if PASCAL ran it within timeout_s, `off` if not "
               "(ask a person to switch PASCAL to MI Mode), or `busy` if another script "
               "is running and the probe cannot tell.",
           timeout_s=MOTION_TIMEOUT_S),

        # --- fast hardware control
        Op("set_target", SetTarget, Ack,
           doc="Revolve the carousel to a target slot (A-F, Clear, Monitor) and set "
               "target rotation and twist to AUTO, so the target rasters whenever the "
               "laser fires. The modes cannot be turned off from here. Refused while "
               "the motor holding lock is released (see is_motor_free).",
           journal=True, timeout_s=MOTION_TIMEOUT_S),
        Op("start_mi_logging", StartMiLogging, LoggingStatus,
           doc="Start the growth log at a fixed whole-second row interval: under "
               "pld-service the chamber node's log of the service's state, under "
               "--use-legacy-mi-mode PASCAL's data logging (`Log Interval` + `Data "
               "Logging File=`, which powers up at 60s). Pass interval_s=1 for a growth. "
               "Empty file_name lets the driver name the file. Returns the file name and "
               "interval in use. PASCAL will not switch files while a logger is already "
               "open -- call stop_mi_logging first if one might be running.",
           journal=True),
        Op("stop_mi_logging", Empty, Ack,
           doc="Close the open growth-log file (PASCAL's `Data Logging OFF` under "
               "--use-legacy-mi-mode). There, it is needed before start_mi_logging can "
               "point at a new file: an already-running logger acks without switching.",
           journal=True),
        Op("move_mask_to_position", MoveMask, Ack,
           doc="Move the mask to a position in mm. Blocks until the move completes. Optional speed_mm_s and "
               "accel_mm_s_per_ms set the mask's speed and ACC/DEC for this move only (pld driver; 0 = "
               "pld-service's default). Refused while the motor holding lock is released.",
           journal=True, timeout_s=MOTION_TIMEOUT_S),
        Op("move_rheed_to_position", MoveTo, Ack, journal=True, timeout_s=MOTION_TIMEOUT_S),
        Op("rheed_on", RheedOn, RheedGunResult,
           doc="Switch the RHEED gun on at the lab's operating values (pld driver only): "
               "filament on, HV on (25 kV), then the filament raised in 50 mA steps from "
               "1800 mA (2100 mA at most) until the emission's first two digits stop "
               "changing; that last step is taken back. With by_person (the lab's "
               "default) it stops after filament and HV and raises a `rheed_emission` "
               "pending confirmation instead: show the person the table, then "
               "rheed_emission_step_up per step until they say stop, then finish_rheed_on. "
               "The result's message ends with the filament-vs-emission table, then focus and deflection X/Y. Each "
               "value can be overridden for this call; focus and deflection depend on the "
               "alignment. Waits for every readback, about 2-4 minutes from off. Fails "
               "with what was and was not reached, leaving the gun as it got. Beam "
               "position screws, incidence angle and camera gain stay with a person.",
           journal=True, timeout_s=600.0),
        Op("rheed_emission_step_up", Empty, RheedGunResult,
           doc="During a `rheed_emission` confirmation: the filament up one step (50 mA) "
               "and the emission read after it settles. Returns the table so far and "
               "whether the first two digits changed. Only when the person says so.",
           journal=True, timeout_s=300.0),
        Op("finish_rheed_on", FinishRheedOn, RheedGunResult,
           doc="End a `rheed_emission` confirmation where the person said stop: the "
               "filament stays at filament_ma (one of the tried steps; left out, where it "
               "is), then focus and deflection as rheed_on would.",
           journal=True, timeout_s=300.0),
        Op("rheed_off", Empty, RheedGunResult,
           doc="Switch the RHEED gun's HV and filament off, ramped (pld-service's "
               "rheed_all_off_gentle; pld driver only): about two minutes from full. "
               "The normal way off at the end of a substrate.",
           journal=True, timeout_s=360.0),
        Op("rotate_sample_to", SampleAngle, Ack,
           doc="Rotate the sample stage to an absolute angle in degrees. Blocks until the move completes. Refused "
               "while the motor holding lock is released (see is_motor_free).",
           journal=True, timeout_s=MOTION_TIMEOUT_S),
        Op("rotate_sample_by", SampleAngle, Ack,
           doc="Rotate the sample stage by a signed delta in degrees; negative turns "
               "the other way. Blocks until the "
               "move completes. Refused while the motor holding lock is released.",
           journal=True, timeout_s=MOTION_TIMEOUT_S),
        Op("to_pixel", PixelIndex, PixelMoveResult, journal=True, timeout_s=MOTION_TIMEOUT_S),
        Op("to_current_pixel", Empty, PixelMoveResult, journal=True, timeout_s=MOTION_TIMEOUT_S),
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
               "which drives the control MFC itself. pld-service has no gate: closing "
               "it sets MFC1 to zero, opening it does nothing (set_mfc_flow flows at "
               "once).", journal=True),
        Op("set_pressure", SetPressure, Ack,
           doc="Set the chamber pressure in Torr. Under pld-service (the default) it "
               "sets the service's pressure-control setpoint, which picks its regime "
               "(<= 2e-4 low, >= 3e-3 high; between is refused) and takes effect once "
               "set_pressure_control(on=True) is on. Under --use-legacy-mi-mode on the "
               "real chamber a person sets it by hand: this raises a `pressure` pending "
               "confirmation for them, resolved with confirm once they have; in the "
               "simulator it sets the setpoint directly. Read it back with "
               "get_current_pressure.", journal=True),
        Op("set_pressure_control", SetPressureControl, Ack,
           doc="Turn closed-loop pressure control on or off. On: the controller owns "
               "the control MFC and holds set_pressure's setpoint. Off: flow reverts "
               "to whatever set_mfc_flow last set, gated by set_mfc_control.", journal=True),
        Op("initiate_heating_laser", Empty, Ack, journal=True),
        Op("turn_off_heating_laser", TurnOffHeatingLaser, Ack,
           doc="Turn the heating laser off. Refused while the substrate reads above "
               "165 C (experiment.bounds.heating_laser_off_max_temperature): cutting "
               "the laser hot cools the sample all at once. cool_down first and wait "
               "until the temperature reads cold -- a to_temperature with wait=false "
               "ends its task before the substrate gets there. "
               "force=true overrides.", journal=True),
        Op("start_storage", StartStorage, StorageResult, journal=True),
        Op("end_storage", EndStorage, StorageResult, journal=True),
        Op("finish_experiment_record", FinishExperimentRecord, ExperimentRecordId, journal=True),

        # --- long-running automatic ops: return immediately, tracked via
        # state.current_task (see TaskEvent on the update channel).
        Op("to_temperature", ToTemperature, TaskAck,
           doc="Ramp to a temperature and engage PID. Requires the heating laser to "
               "be on already -- call initiate_heating_laser first, or this fails "
               "immediately rather than setting a setpoint no current can reach. "
               "wait=true (the default): the task ends when the substrate arrives; under "
               "--use-legacy-mi-mode PASCAL runs no other command (moves, target, "
               "shutter) until then. "
               "wait=false: the task ends once the ramp is under way and the chamber is "
               "free for other commands; read get_current_temperature until it is "
               "within tolerance before relying on it, and never follow it with "
               "turn_off_heating_laser while still hot. "
               "A setpoint below the PID-engage threshold is not holdable: from a hot "
               "chamber it is commanded as a cooldown (no PID), and from an already-"
               "cold one it is a no-op, since a room-temperature growth runs with the "
               "diode off.", journal=True),
        Op("cool_down", CoolDown, TaskAck, journal=True),
        Op("perform_preablation", PerformPreablation, TaskAck,
           doc="Pre-ablate a target behind the mask before depositing from it. Leave "
               "num_pulse and frequency out: the amount set for the target's material "
               "(set_target_preablation) is used, and it is refused when none is set yet "
               "(ask the person, then set it) or the target is set to 0 (never "
               "pre-ablate it). Give num_pulse only when the person asked for a "
               "different amount this time.", journal=True),
        Op("perform_deposition", PerformDeposition, TaskAck, journal=True),
        Op("anneal", Anneal, TaskAck, journal=True),
        Op("stop_deposition", StopDeposition, StopDepositionResult,
           doc="Abort the deposition or preablation now: ends the running "
               "perform_deposition/perform_preablation task (its task_result is ok=false, "
               "aborted=true) and stops the laser (chamber.pld.stop_fire). Also stops a "
               "pulse train with no task behind it, and is harmless when nothing is "
               "firing. The shutter, mask, target spin and logging are left as they are; "
               "a preablation stopped this way leaves the mask at its block position. "
               "The run is over: do not fire again to make up the pulses. "
               "Refused under --use-legacy-mi-mode, which cannot stop a pulse train "
               "(use PASCAL's own STOP).", journal=True, timeout_s=60.0),
        Op("cancel_task", CancelTask, CancelTaskResult,
           doc="End the running long task, whatever it is (state.current_task); its "
               "task_result is ok=false, aborted=true. A deposition or preablation is "
               "stopped as stop_deposition stops it, laser first. A to_temperature, "
               "cool_down or anneal switches the PID off, leaving the heating LD at the "
               "current it carries now: no new setpoint, so nothing heats or cools "
               "harder than it was. The substrate then settles where that current puts "
               "it; to_temperature takes it from there. A heater already on manual "
               "current is left as it is. task_id, if given, cancels only that task. Nothing running: "
               "nothing done. Refused under --use-legacy-mi-mode, which cannot "
               "interrupt a PASCAL command.", journal=True, timeout_s=60.0),

        # --- gated: laser pulse energy (unread physical meter)
        Op("begin_set_laser_power", BeginSetLaserPower, Ack,
           doc="Set the laser pulse energy at the meter, `laser_power` in mJ (the name is "
               "historical). Under pld-service no one reads the meter: the excimer runs in "
               "energy mode at laser_power divided by today's laser test ratio (meter / "
               "source), and no confirmation is raised. Refused when there was no laser "
               "test today: run begin_laser_test first. Skipped when this energy is "
               "already set from today's test (force=true sets it again). "
               "Under --use-legacy-mi-mode: closes the sample shutter, turns the carousel "
               "to Clear and raises a `laser_power` confirmation for the person reading "
               "the meter; call nothing else until confirm_laser_power.",
           journal=True, timeout_s=MOTION_TIMEOUT_S),
        Op("confirm_laser_power", ConfirmLaserPower, LaserPowerResult,
           doc="Answer begin_set_laser_power with the pulse energy read off the meter, mJ "
               "(legacy MI mode only).",
           journal=True),
        # --- gated: the daily laser test (pld-service only)
        Op("begin_laser_test", Empty, Ack,
           doc="Start the daily laser test, once a day before the first deposition: "
               "closes the sample shutter, turns the carousel to Clear (the energy meter "
               "is at the exit port) and runs the excimer in HV mode at 21 kV. Raises a "
               "`laser_test` pending confirmation: the person either says yes, and you "
               "call fire_laser_test (100 pulses at 1 Hz), or fires the laser "
               "themselves. Then confirm_laser_test with the meter's average. Call "
               "nothing that moves the carousel in between.",
           journal=True, timeout_s=MOTION_TIMEOUT_S),
        Op("fire_laser_test", Empty, TaskAck,
           doc="Fire the laser test's 100 pulses at 1 Hz into the meter, after the person "
               "said yes; about 100 s. A task: its result carries source_mj, the "
               "excimer's average energy while it fired. stop_deposition stops it.",
           journal=True),
        Op("confirm_laser_test", ConfirmLaserTest, LaserTest,
           doc="Finish the laser test with the meter's average energy, mJ. The source "
               "energy is the excimer's own average while Lumi fired; if the person "
               "fired, it is the laser's last reading, or pass source_mj. Records the "
               "test (meter / source ratio) in growth.db and the recording; "
               "begin_set_laser_power uses it for the rest of the day.",
           journal=True),
        Op("list_laser_tests", ListLaserTests, LaserTestList,
           doc="The laser tests, newest first; `today` marks one begin_set_laser_power can use."),
        # --- sequences: step lists pld-service runs (pld driver only)
        Op("save_sequence", SaveSequence, SequenceInfo,
           doc="Store a step list for pld-service to run. Different steps under an existing "
               "name become its next version (a version never changes); the same steps "
               "return the version they already are. A new version is unapproved: a person "
               "reviews it once (approve_sequence) before it can run.",
           journal=True),
        Op("list_sequences", ListSequences, SequenceList,
           doc="Stored sequences, every version, with who saved and who approved each."),
        Op("get_sequence", GetSequence, SequenceInfo,
           doc="One version with its full step list (the latest when version is left out)."),
        Op("approve_sequence", SequenceRef, SequenceInfo,
           doc="A person approves one exact version after reviewing its steps; from then on it "
               "runs without anyone. Refused for the agent (actor mcp:...) and without a named "
               "actor: the agent shows the person the steps and asks them to approve in the Deck.",
           journal=True),
        Op("run_sequence", RunSequence, TaskAck,
           doc="Run one approved version on pld-service; refused if that version is not "
               "approved. Returns a TaskAck at once; progress is `sequence` in the driver "
               "state and each step goes in the step journal. meter_energy steps need today's "
               "laser test. While it runs pld-service refuses every other operation, from every "
               "client, except stop_fire, stop_motors (which cancels it), the RHEED all-offs and "
               "pause/cancel. A failed step "
               "ends the run; nothing is undone or retried. stop_deposition and cancel_task "
               "cancel it. Needs a substrate and project loaded (no_sample=true for a rehearsal). "
               "Each fire with the shutter open grows a layer of the selected target's material "
               "(consecutive fires on one material merge): a sequence_layer journal row and a growth "
               "record with the sequence ref, as perform_deposition plus finish_experiment_record "
               "would. record={project_name, ...} wraps the run in a recording.",
           journal=True, timeout_s=MOTION_TIMEOUT_S),
        Op("pause_sequence", Empty, Ack,
           doc="Pause the running sequence: a wait stops counting, a waited fire stops its "
               "train (resume fires the pulses left), any other step finishes first.",
           journal=True),
        Op("resume_sequence", Empty, Ack, doc="Carry on a paused sequence (also ends a `pause` step).",
           journal=True),
        Op("cancel_sequence", Empty, Ack,
           doc="End the running sequence now; a fire train stops, a move or ramp under way "
               "carries on, nothing is undone.",
           journal=True),
        # --- per-target pre-ablation
        Op("list_target_preablation", Empty, TargetPreablationList,
           doc="The pre-ablation amount set for each target material. A material not "
               "listed has none yet: ask the person before pre-ablating it."),
        Op("set_target_preablation", SetTargetPreablation, TargetPreablation,
           doc="Set how much to pre-ablate a target material before each deposition, as "
               "the person said (num_pulse 0 = never pre-ablate it). Kept until set "
               "again; perform_preablation uses it.",
           journal=True),
        # --- the excimer's own settings (pld-service only)
        Op("set_laser_energy", SetLaserEnergy, LaserSettings,
           doc="Run the excimer at constant energy: switches it to energy mode (EGY PGR) "
               "and sets the energy set point, mJ. Changes the beam, so a pulse energy "
               "confirmed with begin_set_laser_power is forgotten and the next check "
               "measures again. Refused while the laser fires or a task holds the motors "
               "(a deposition), and under --use-legacy-mi-mode, which cannot reach the "
               "excimer: set it on the laser's own PC there. If the second PC has the "
               "excimer's settings port, it is taken for the change and handed back.",
           journal=True, timeout_s=60.0),
        Op("set_laser_hv", SetLaserHv, LaserSettings,
           doc="Run the excimer at constant HV: switches it to HV mode (HV PGR) and sets "
               "the HV set point, kV. Otherwise as set_laser_energy.",
           journal=True, timeout_s=60.0),

        # --- gated: mask-center calibration / check (visual judgement)
        Op("begin_align_center_mask", Empty, Ack, journal=True, timeout_s=MOTION_TIMEOUT_S),
        Op("confirm_center_mask", ConfirmCenterMask, Ack, journal=True, timeout_s=MOTION_TIMEOUT_S),
        Op("begin_check_mask_center", Empty, Ack, journal=True, timeout_s=MOTION_TIMEOUT_S),
        Op("confirm_mask_center", ConfirmMaskCenter, PendingStatus, journal=True, timeout_s=MOTION_TIMEOUT_S),
        # --- automatic: the same calibration, read off the chamber camera
        Op("set_center_mask_pos", CenterMaskPos, Ack,
           doc="Set center_mask_pos without moving the mask -- e.g. to keep a "
               "report-only (apply=false) auto-align result. Saved, like every change "
               "to it, so it survives a node restart.", journal=True),
        Op("auto_align_center_mask", AutoAlignMaskCenter, TaskAck,
           doc="Scan Mask1 around center_mask_pos (or center_mm), watching the fiducial marker tagged "
               "'mask-center' (the sample's centre), locate the slit from the "
               "intensity-vs-position curve, then re-scan just the slit, finer each pass, "
               "until the centre settles. Sets center_mask_pos to it and leaves the mask "
               "there. Long-running: the MaskAlignResult (centre, each pass's fit, every "
               "scanned point) arrives as the task_result. If no marker is tagged "
               "'mask-center' it first opens a 'fiducial_role' pending confirmation and "
               "waits for one to be tagged (chamber.fiducial.set_role), then continues; "
               "`confirm` on that confirmation cancels the alignment.", journal=True,
           result=MaskAlignResult),

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
           doc="Every sample on a substrate, or on all of them when substrate_id is "
               "left out (it does not default to the current substrate), with its "
               "position and state. A sample exists per growable position from registration, so "
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
               "sorts on; `detail` carries named scalars; `series` carries curves (a scan, "
               "R(T), a loop). Files go on afterwards with attach_measurement_file."),
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
