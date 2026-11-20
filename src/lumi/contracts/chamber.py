"""The Pascal chamber node: growth log, chamber config, the chamber driver (pld-service,
or MI mode as the legacy fallback), chamber camera, fiducial markers."""

from __future__ import annotations

from .payloads.camera import CameraConfig, CameraState, ImageMeta, JpegMeta
from .payloads.chamber import (
    AllConfigs,
    ChamberConfigState,
    ChamberLogState,
    ConfigEntry,
    ConfigQuery,
    ConfigSection,
    ConfigSections,
    ListLogFiles,
    LogBatch,
    LogEntry,
    LogFileList,
    LogSeries,
    LogWindowQuery,
    MICommands,
    MIExecution,
    MIExecutionList,
    MIModeState,
    SectionQuery,
)
from .payloads.common import Ack, Empty
from .payloads.fiducial import (
    FiducialMarker,
    FiducialState,
    MarkerHistory,
    MarkerHistoryQuery,
    MarkerId,
    MarkerList,
    MarkerStatsSample,
    RoleAssignment,
    RoleMap,
    RoleQuery,
)
from .payloads.pld import (
    PldAutoMotionRequest,
    PldAxisRequest,
    PldChamberState,
    PldControlReply,
    PldControlRequest,
    PldCurrentLimitsRequest,
    PldCurrentRequest,
    PldEventList,
    PldEventQuery,
    PldFireRequest,
    PldFlowLimitsRequest,
    PldFlowRequest,
    PldIonGaugeRequest,
    PldPumpRequest,
    PldLaserEnergyRequest,
    PldLaserHvRequest,
    PldLaserModeRequest,
    PldLaserOnRequest,
    PldLoggingRequest,
    PldPositionRequest,
    PldPressureRequest,
    PldReply,
    PldShutterRequest,
    PldState,
    PldSwitchRequest,
    PldTargetRequest,
    PldTemperatureRequest,
    PldValveRequest,
)
from .spec import Capability, Codec, EquipmentContract, Kind, Op, StreamSpec

LOG = Capability(
    name="log",
    kind=Kind.DUPLEX,
    doc="The chamber's growth log, tailed from disk.",
    state=ChamberLogState,
    ops=(
        Op("log", Empty, LogBatch, doc="Latest log rows."),
        Op("list_log_files", ListLogFiles, LogFileList,
           doc="The log files in the chamber's log folder, newest name first, with the "
               "time span each covers."),
        Op("log_window", LogWindowQuery, LogSeries,
           doc="The log over a time window, as columns, across however many files it "
               "spans -- or one named file. For charting a past growth."),
    ),
    stream=StreamSpec("log", LogEntry),
)

CONFIG = Capability(
    name="config",
    kind=Kind.RPC,
    doc="PLDconfig.ini, parsed and watched for changes.",
    state=ChamberConfigState,
    ops=(
        Op("get_all_config", Empty, AllConfigs),
        Op("get_config", ConfigQuery, ConfigEntry),
        Op("get_configs_by_section", SectionQuery, ConfigSection),
        Op("get_sections", Empty, ConfigSections),
    ),
)

MI_MODE = Capability(
    name="mi_mode",
    kind=Kind.PUBSUB,
    doc="Machine-instruction mode: submit a command script, get an immediate "
        "acknowledgement, and receive the execution result later on the update "
        "channel, correlated by commands_uuid. The one genuinely pub/sub capability. "
        "The legacy driver, through PLD Controller: live only when the chamber node runs "
        "--use-legacy-mi-mode, and refused otherwise (pld drives the chamber).",
    state=MIModeState,
    ops=(
        Op("register_commands", MICommands, MIExecution,
           doc="Queue a script. Returns immediately; completion arrives as an update."),
        Op("list_execution", Empty, MIExecutionList),
    ),
    update=StreamSpec("execution", MIExecution),
)

#: How long a caller waits on an op that can block until the chamber gets there
#: (`wait=true`). A move is well under this; a 20 C/min ramp to 800 C, a long pulse
#: train or a pressure regime switch is not, hence the longer one.
_MOVE_S = 600.0
_LONG_S = 4 * 3600.0


def _pld_op(name: str, request, doc: str, *, timeout_s: float | None = None) -> Op:
    return Op(name, request, PldReply, doc=doc, journal=True, timeout_s=timeout_s)


PLD = Capability(
    name="pld",
    kind=Kind.DUPLEX,
    doc="The chamber driver: pld-service, the REA control service on the PLD PC, which "
        "owns the serial ports (PLC, gauges, pyrometer, excimer) and enforces every "
        "safety rule itself. One op per service operation. Every op answers "
        "{ok, message}; ok=false is a refusal (an interlock, a limit, read-only "
        "hardware, another client holding control) with the reason in message. "
        "Operations need control, which this node takes as client 'lumi-pascal' on "
        "first use. When the node runs --use-legacy-mi-mode every op here is refused "
        "and mi_mode drives the chamber instead.",
    state=PldState,
    ops=(
        Op("chamber_state", Empty, PldChamberState,
           doc="The latest poll of the chamber: valves, pressures, heater, motion, the "
               "laser trigger train, gas and pressure control, interlocks, the excimer. "
               "`read_only` true means the service was started on hardware without "
               "--allow-operations: every operation will be refused."),
        Op("recent_events", PldEventQuery, PldEventList,
           doc="The service's latest discrete events (valve_changed, move_done, "
               "interlock, laser_done, error, control), oldest first."),
        Op("acquire_control", PldControlRequest, PldControlReply, journal=True,
           doc="Take control of the chamber as 'lumi-pascal'. Operations do this "
               "themselves; call it to see who holds control, or with force=true to take "
               "it from another client (logged by the service; never the default)."),
        Op("release_control", Empty, PldControlReply, journal=True,
           doc="Hand control back, e.g. so the REA GUI can drive. Release it when a run ends."),
        _pld_op("set_valve", PldValveRequest, "Open or close a valve by its reference name.",
                timeout_s=_MOVE_S),
        _pld_op("select_target", PldTargetRequest, "Revolve the carousel to target A..F or Clear.",
                timeout_s=_MOVE_S),
        _pld_op("set_mask_position", PldPositionRequest, "Mask1 to an absolute position, mm.",
                timeout_s=_MOVE_S),
        _pld_op("set_sample_position", PldPositionRequest, "Sample rotation to an absolute angle, 0..360 degrees.",
                timeout_s=_MOVE_S),
        _pld_op("set_rheed_gun_x", PldPositionRequest, "RHEED gun X to an absolute position, mm.",
                timeout_s=_MOVE_S),
        _pld_op("home_axis", PldAxisRequest, "Home TG, Mask1 or RHEED gun X.", timeout_s=_MOVE_S),
        _pld_op("set_target_spin", PldSwitchRequest, "Target spin on or off."),
        _pld_op("set_target_twist", PldSwitchRequest, "Target twist on or off."),
        _pld_op("set_motor_free", PldSwitchRequest, "Release (on) or engage (off) the motor holding lock."),
        _pld_op("set_target_auto_motion", PldAutoMotionRequest,
                "Auto mode: the chosen motions go on with each fire_laser and off when the "
                "train ends. Service default: spin on, twist off."),
        _pld_op("set_heater_key", PldSwitchRequest, "Heater LD key (MI: Heating Laser Lock ACTIVE/LOCKED)."),
        _pld_op("set_heater_start", PldSwitchRequest, "Heater LD start (MI: Heating Laser ON/OFF)."),
        _pld_op("set_heater_threshold", PldSwitchRequest, "Heater LD idle threshold current (MI: Heating Laser Threshold)."),
        _pld_op("set_heating_current", PldCurrentRequest, "Manual LD current, A. Switches the temperature PID off."),
        _pld_op("set_heater_current_limits", PldCurrentLimitsRequest, "Limits on the PID output current, A."),
        _pld_op("set_temperature", PldTemperatureRequest,
                "PID towards `celsius` at `ramp_c_per_min` (0 = jump); with wait, returns "
                "once within `tolerance`.", timeout_s=_LONG_S),
        _pld_op("set_temperature_control", PldSwitchRequest,
                "on = PID holding the present temperature (or the last setpoint); off = "
                "manual at the present current."),
        _pld_op("set_sample_shutter", PldShutterRequest, "Sample shutter in front of the holder, or away.",
                timeout_s=_MOVE_S),
        _pld_op("fire_laser", PldFireRequest,
                "A PLC trigger train of `pulses` at `rate_hz`. The excimer makes light only "
                "when it is ON; this does not check it.", timeout_s=_LONG_S),
        _pld_op("stop_fire", Empty,
                "Stop the present pulse train now (PLD Controller's stop): pulses fired are kept, "
                "the motions the fire started go off. Needs no control. A waited fire_laser it "
                "cuts short answers ok=false, 'stopped by request: fired N of M pulses' -- an "
                "abort, never re-fired. Not firing: ok=true, nothing sent. The excimer itself "
                "is laser_off.", timeout_s=60),
        _pld_op("set_mfc_flow", PldFlowRequest, "MFC setpoint, sccm (MFC1 only in this chamber)."),
        _pld_op("set_pressure", PldPressureRequest,
                "Pressure-control setpoint, Torr. The setpoint picks the regime: low "
                "(<= 2e-4, IG1, flow table) or high (3e-3..1, CDG, PID); between them is "
                "refused. Switching regimes moves valves and the ion gauge first.",
                timeout_s=_LONG_S),
        _pld_op("set_pressure_control", PldSwitchRequest, "Pressure control on or off."),
        _pld_op("set_pressure_flow_limits", PldFlowLimitsRequest, "Flow limits for pressure control, sccm."),
        _pld_op("tune_pressure", PldSwitchRequest, "Relay auto-tune of the high-regime PID (off cancels)."),
        _pld_op("set_ion_gauge", PldIonGaugeRequest,
                "IG1 or IG3 filament on or off. On is refused above the guard gauge's limit "
                "(IG1: CDG10, IG3: TCG31), and a filament is switched off when it rises above."),
        _pld_op("tune_heater", PldSwitchRequest,
                "Relay auto-tune of the temperature PID at its present setpoint (needs PID on; "
                "off cancels). ~3 cycles of +-0.2 A, then the PID runs on the new gains until "
                "the service restarts. A new setpoint, manual current or an interlock ends it."),
        _pld_op("auto_vent", Empty,
                "Load-lock Auto Vent, step by step as the PLC's own sequence. Refused unless GV1 "
                "is closed, the hatch closed and the PLC's vent condition on. Progress comes as "
                "load_lock events; the vents stay open until the hatch is opened by hand."),
        _pld_op("auto_vac", Empty,
                "Load-lock Auto Vac. Refused as auto_vent, and also with DP3 off, TMP3 not at "
                "speed or MV3 already open."),
        _pld_op("cancel_load_lock", Empty, "Stop the running Auto Vent / Auto Vac. Not running: nothing sent."),
        _pld_op("set_pump", PldPumpRequest,
                "Start or stop a pump. Refused: a TMP start while its dry pump is off, a dry-pump "
                "stop while a TMP it backs runs (DP1: TMP1, TMP20; DP2: TMP21; DP3: TMP3)."),
        _pld_op("take_laser_control", Empty,
                "Take the excimer's settings port from the second PC (CM=2). Every other "
                "laser op is refused without it."),
        _pld_op("laser_on", PldLaserOnRequest, "Excimer preflight, then OPMODE=ON (about 8 s in OFF,WAIT).",
                timeout_s=60.0),
        _pld_op("laser_standby", Empty, "Excimer to STANDBY."),
        _pld_op("laser_off", Empty, "Excimer OFF."),
        _pld_op("set_laser_mode", PldLaserModeRequest, "Excimer running mode: hv (HV PGR) or energy (EGY PGR)."),
        _pld_op("set_laser_hv", PldLaserHvRequest, "Excimer HV set point, 18.5..25.5 kV (hv mode only)."),
        _pld_op("set_laser_energy", PldLaserEnergyRequest, "Excimer energy set point, 100..400 mJ."),
        _pld_op("release_laser", Empty, "Hand the excimer's settings port back to the second PC (CM=1)."),
        Op("start_logging", PldLoggingRequest, PldReply, journal=True,
           doc="Start a new growth-log CSV in the chamber's log folder, one row every "
               "interval_s, written by this node from the service's state (PLD "
               "Controller's columns). `message` is the file's path."),
        Op("stop_logging", Empty, PldReply, journal=True,
           doc="Close the growth-log CSV. Nothing is logged until start_logging."),
    ),
    stream=StreamSpec("state", PldChamberState,
                      doc="Every poll of the chamber, as chamber_state returns it, at about 1 Hz."),
)

CAMERA = Capability(
    name="camera",
    kind=Kind.DUPLEX,
    doc="Chamber-facing webcam. Shares the RHEED camera's ops -- which is why there "
        "is one camera handler and one generated camera client, not two -- but "
        "streams MJPEG rather than raw arrays.",
    state=CameraState,
    ops=(
        # Lossless, and deliberately so: this is the op to call when the pixels are
        # the point (a saved still, a measurement). The *stream* is for looking at.
        Op("image", Empty, ImageMeta, response_codec=Codec.NPY),
        Op("get_camera_config", Empty, CameraConfig),
        Op("update_camera_config", CameraConfig, Ack),
    ),
    # MJPEG, not raw arrays. At 640x480x3 and 25fps the NPY stream was ~23 MB/s per
    # subscriber, which saturated the websocket bridge and cost the browser a full
    # decode-and-widen on the main thread for every frame.
    #
    # JPEG rather than H.264 (which is what rheed.video does) because every stage of
    # this pipeline drops frames on purpose -- the camera's fan-out queues, the
    # bridge's per-subscription prefetch, and the frontend's keep-only-the-newest
    # buffer. Dropping an independent JPEG costs one frame; dropping an H.264
    # fragment corrupts playback until the next keyframe, and dropping an init
    # segment means the player never starts at all. It also keeps a subscriber
    # stateless (no init segment to fetch first, so late joiners just work), keeps
    # each frame's uuid/time_stamp aligned 1:1 with its pixels, and has no muxer
    # timeline to desynchronise -- the exact failure that killed the RHEED encoder
    # thread with a non-monotonic DTS.
    stream=StreamSpec("frame", JpegMeta, codec=Codec.RAW),
)

FIDUCIAL = Capability(
    name="fiducial",
    kind=Kind.DUPLEX,
    doc="Fiducial markers -- crosses, rectangles, polygons -- the operator sets on the "
        "chamber webcam image, and the intensity statistics (mean, min, max, std) of the "
        "pixels under each one. Geometry is in camera-frame pixels, the same space the "
        "`camera` stream is encoded in, so a frontend draws it straight over the feed. "
        "The statistics are for calibration: a marker's intensity trace dips as the mask "
        "edge crosses it, which locates the mask.",
    state=FiducialState,
    ops=(
        Op("list_markers", Empty, MarkerList,
           doc="Every marker, with the frame size they were drawn against."),
        Op("set_marker", FiducialMarker, Ack,
           doc="Add a marker, or replace the one with this id. Persisted across restarts."),
        Op("remove_marker", MarkerId, Ack),
        Op("marker_stats", Empty, MarkerStatsSample,
           doc="Statistics of every marker on the latest frame."),
        Op("marker_history", MarkerHistoryQuery, MarkerHistory,
           doc="One marker's retained intensity trace, oldest first."),
        Op("set_role", RoleAssignment, Ack,
           doc="Name a marker for a purpose (e.g. role='mask-center'), so an "
               "automated step can look it up by what it is for. Replaces whatever "
               "marker the role previously pointed at."),
        Op("remove_role", RoleQuery, Ack),
        Op("list_roles", Empty, RoleMap,
           doc="Every role -> marker_id assignment, plus `known`: the predefined roles "
               "something in the system reads, to offer as choices when tagging."),
    ),
    stream=StreamSpec("stats", MarkerStatsSample),
)

CHAMBER = EquipmentContract(
    name="chamber",
    exchange="CHAMBER",
    version="2.0",
    capabilities=(LOG, CONFIG, PLD, MI_MODE, CAMERA, FIDUCIAL),
)
