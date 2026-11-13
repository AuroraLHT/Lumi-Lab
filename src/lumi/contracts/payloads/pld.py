"""The PLD chamber driver's payloads: pld-service's gRPC API, as bus models.

pld-service (the REA repo's Rust control service on the PLD PC) owns the chamber's
serial ports and enforces every safety rule itself. These models mirror its
`pld.v1` messages (src/lumi/pascal/pld/proto/pld.proto) one for one, field names
included, so a reader of either side can find the other. Two deliberate differences:

- A reading the service reports as NaN (the pyrometer below ~140 C, IG1 warming up, a
  setpoint outside PID mode) is `None` here. JSON has no NaN.
- `raw_words` (every polled PLC word) is not carried: many of its bits are still
  unconfirmed, and nothing should be built on them without asking the REA side first.

An operation answers `PldReply`. `ok=False` is a refusal -- the service said no, and
`message` says why (an interlock, a limit, `read_only`, another client holding
control). A refusal is an answer, not an error, so it is returned rather than raised:
the caller decides whether to stop.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .common import ServerStateBase

# --- state ------------------------------------------------------------------------


class PldPressures(BaseModel):
    ig1: float | None = Field(None, description="Main chamber ion gauge, Torr; 0 with the filament off")
    ig3: float | None = Field(None, description="Load-lock ion gauge, Torr")
    cdg10: float | None = None
    cdg11: float | None = None
    tcg: list[float | None] = Field(default_factory=list, description="TCG1/2/3 (order not yet confirmed)")
    tcg31: float | None = None
    ig1_on: bool = False
    ig3_on: bool = False


class PldHeater(BaseModel):
    temperature: float | None = Field(None, description="Pyrometer, C; None below ~140 C")
    emissivity: float | None = None
    current_set: float | None = Field(None, description="Last current written to the LD, A")
    current_monitor: float | None = None
    power_on: bool = False
    ready: bool = Field(False, description="Valve interlock satisfied")
    started: bool = False
    temperature_setpoint: float | None = Field(None, description="The ramped setpoint the PID follows now")
    pid_on: bool = False
    key_on: bool = False
    start_on: bool = False
    threshold_on: bool = False
    current_min: float | None = Field(None, description="PID output limits, A (min: the 6 A threshold current)")
    current_max: float | None = None
    temperature_goal: float | None = Field(None, description="Final PID setpoint, C")
    ramp_c_per_min: float = Field(0.0, description="0 = jump")
    auto_tune: bool = Field(False, description="tune_heater is running")
    kp: float | None = Field(None, description="PID gains in use: A/C, A/(C s), A s/C")
    ki: float | None = None
    kd: float | None = None
    writes_failing: bool = Field(False, description="The per-cycle LD current writes are failing")
    tune_cycles_done: int = Field(0, description="tune_heater: relay cycles done (0 when not tuning)")
    tune_cycles: int = Field(0, description="tune_heater: relay cycles in all (0 when not tuning)")
    warming_up: bool = Field(
        False, description="set_temperature from below 200 C: LDmin (7.6 A) held until then, PID after")


class PldMotion(BaseModel):
    target_angle: float | None = None
    target: str = Field("", description="A..F, Clear, or empty between slots")
    sample_angle: float | None = None
    mask_position: float | None = None
    rheed_gun_x: float | None = None
    axes: dict[str, str] = Field(default_factory=dict, description="axis -> IDLE/MOVING/HOMING/SETTLING")
    target_spinning: bool = False
    target_twisting: bool = False
    motor_free: bool = Field(False, description="Holding lock released: axes back-driveable, not under servo")
    auto_spin: bool = False
    auto_twist: bool = False


class PldLaser(BaseModel):
    """The PLC's trigger train (FireLaser), not the excimer itself."""

    status: str = Field("", description="idle, firing, finished")
    pulses_fired: int = 0
    missed_shots: int = 0
    rate_hz: float = Field(0.0, description="The current or last train's rate as the PLC runs it "
                           "(1000 / the whole-ms period); 0 before the first train. A train "
                           "started by another program than pld-service does not show here.")
    pulses_requested: int = Field(0, description="The current or last train's requested pulse "
                                  "count; 0 before the first train.")
    stop_requested: bool = Field(False, description="StopFire was sent during the present train "
                                 "(cleared by the next fire)")


class PldLaserCode(BaseModel):
    code: int
    kind: str = Field("", description="interlock-hw, interlock-sw, warning")
    meaning: str = ""


class PldExcimer(BaseModel):
    """The COMPexPro as its own polls report it."""

    present: bool = Field(False, description="The service has a laser link")
    connected: bool = False
    opmode: str = ""
    waiting: bool = Field(False, description="OFF,WAIT: switching on, about 8 s")
    status: str = ""
    codes: list[PldLaserCode] = Field(default_factory=list)
    mode: str = Field("", description="Running mode, e.g. HV PGR, EGY PGR")
    hv: float | None = Field(None, description="HV set point, kV")
    energy_set: float | None = Field(None, description="Energy set point, mJ")
    energy: float | None = Field(None, description="Measured average while running, mJ")
    control: bool = Field(False, description="The laser takes settings from our port; false = the second PC has it")
    trigger: str = ""
    reprate: float | None = None
    counter: int = 0
    tube_temperature: float | None = None
    tube_pressure: float | None = None
    version: str = ""
    hv_min: float | None = None
    hv_max: float | None = None
    energy_min: float | None = None
    energy_max: float | None = None


class PldFlowPoint(BaseModel):
    sccm: float
    torr: float


class PldGas(BaseModel):
    mfc1_setpoint: float | None = None
    mfc1_flow: float | None = None
    pumping: str = Field("", description="full, throttled, closed (from MV10 / MV11)")
    pressure_control: bool = False
    pressure_setpoint: float | None = Field(None, description="Torr; None when not set")
    pressure_gauge: str = Field("", description="IG1, CDG10 or CDG11")
    pressure_mode: str = Field("", description="low (table), high (PID), auto-tune, switching to low|high")
    flow_min: float | None = None
    flow_max: float | None = None
    pressure_kp: float | None = None
    pressure_ki: float | None = None
    pressure_kd: float | None = None
    pressure_table: list[PldFlowPoint] = Field(default_factory=list)
    pressure_switch_step: str = ""


class PldPump(BaseModel):
    running: bool = Field(False, description="The PLC run bit (TMP1 keeps it ~4.5 min while it spins down)")
    at_speed: bool = Field(False, description="TMPs: NOR bit; dry pumps: running past the ~12 s acceleration")
    state: str = Field("", description="off, starting, at speed, stopping")


class PldLoadLock(BaseModel):
    hatch_closed: bool = False
    ready: bool = Field(False, description="The PLC's vent condition: a sequence may start")
    sequence: str = Field("", description='"", "auto vent" or "auto vac" (the service\'s or the PLC\'s own)')
    step: str = Field("", description="What the sequence is doing now")
    pump_down_complete: bool = False
    below_sp1: bool = Field(False, description="TCG31 below ~4e-2 Torr")
    below_sp2: bool = Field(False, description="TCG31 below SP2 (~400 Torr rising, ~55 falling)")
    ig3_disabled: bool = False
    by_service: bool = Field(False, description="The sequence is the service's (cancel_load_lock stops it)")
    gv1: str = Field("", description="Manual gate valve to the main chamber: closed, open, moving")


class PldSession(BaseModel):
    """pld-service's PLC host-link session."""

    plc_session: bool = Field(False, description="The session is open")
    watchdog: bool = Field(False, description="The service started the PLC watchdog (operations allowed only)")
    link_error: str = Field(
        "", description="Why the PLC/gauge/pyrometer polls are failing (the service reconnects); empty while they work")


class PldLimits(BaseModel):
    """The safety limits pld-service enforces, from its config: check before a refusal."""

    ig1_max_torr: float = Field(0.0, description="IG1 on refused, and switched off, above this on CDG10")
    ig3_max_torr: float = Field(0.0, description="IG3 likewise, on TCG31")
    mv3_max_torr: float = Field(0.0, description="MV3 open refused above this on TCG31")
    heater_max_celsius: float = Field(0.0, description="Above this the heater trips off")
    heater_max_amps: float = Field(0.0, description="LD current ceiling; heater.current_max is the PID's limit within it")


class PldRheed(BaseModel):
    """The RHEED gun's controller, on pld-service's own link. Read-outs are None when
    unknown; ramps run at 30 mA/s and 500 V/s, done when set == target."""

    present: bool = Field(False, description="The service has a RHEED link")
    read_only: bool = Field(False, description="Hardware without --allow-operations: the port is not opened")
    link_ok: bool = Field(False, description="The controller answered the last cycle")
    interlock: bool = Field(False, description="The controller's interlock switched filament and HV off")
    filament_open: bool = Field(
        False, description="Burnt or broken filament suspected (handset E002); filament and HV switched off")
    filament_on: bool = False
    filament_set_ma: float | None = Field(None, description="Sent now, ramping toward the target")
    filament_target_ma: float | None = None
    filament_readback_ma: float | None = None
    filament_last_ma: float | None = Field(None, description="Remembered set point; None = none")
    hv_on: bool = False
    hv_set_kv: float | None = None
    hv_target_kv: float | None = None
    hv_readback_kv: float | None = None
    hv_last_kv: float | None = None
    emission_ua: float | None = None
    focus_on: bool = False
    focus_set_ma: float | None = None
    focus_readback_ma: float | None = None
    x_on: bool = False
    x_set_ma: float | None = None
    x_readback_ma: float | None = None
    y_on: bool = False
    y_set_ma: float | None = None
    y_readback_ma: float | None = None
    last_alert: str = Field("", description="The last link-level alert, kept until the next")
    hv_max_kv: float | None = None
    filament_min_ma: float | None = None
    filament_max_ma: float | None = None
    focus_min_ma: float | None = None
    focus_max_ma: float | None = None
    x_min_ma: float | None = None
    x_max_ma: float | None = None
    y_min_ma: float | None = None
    y_max_ma: float | None = None


class PldChamberState(BaseModel):
    """One poll cycle of pld-service: everything it reads off the chamber."""

    time: float = Field(0.0, description="Unix seconds when the poll cycle finished")
    backend: str = Field("", description="TWIN or HARDWARE")
    connected: bool = Field(False, description="Every port answered in the last cycle")
    controller: str = Field("", description="Client holding control; empty = nobody")
    read_only: bool = Field(
        False, description="Hardware started without --allow-operations: every operation is refused")
    valves: dict[str, bool] = Field(default_factory=dict, description="name -> open")
    pressures: PldPressures = Field(default_factory=PldPressures)
    heater: PldHeater = Field(default_factory=PldHeater)
    motion: PldMotion = Field(default_factory=PldMotion)
    laser: PldLaser = Field(default_factory=PldLaser)
    gas: PldGas = Field(default_factory=PldGas)
    sample_shutter_away: bool = Field(False, description="True = open for deposition")
    interlocks: list[str] = Field(default_factory=list, description="Active interlocks")
    excimer: PldExcimer = Field(default_factory=PldExcimer)
    pumps: dict[str, PldPump] = Field(default_factory=dict, description="TMP1, TMP20, TMP21, TMP3, DP1-DP3")
    load_lock: PldLoadLock = Field(default_factory=PldLoadLock)
    session: PldSession = Field(default_factory=PldSession)
    limits: PldLimits = Field(default_factory=PldLimits)
    rheed: PldRheed = Field(default_factory=PldRheed)


class PldEvent(BaseModel):
    time: float = 0.0
    kind: str = Field("", description="valve_changed, move_done, interlock, laser_done, error, control, ...")
    message: str = ""
    detail: dict[str, str] = Field(default_factory=dict)


class PldEventQuery(BaseModel):
    limit: int = Field(50, ge=1, le=1000, description="At most this many, newest last")


class PldEventList(BaseModel):
    events: list[PldEvent] = Field(default_factory=list)


class PldReadout(BaseModel):
    """What the chamber node knows about its driver connection."""

    driver: Literal["pld", "legacy-mi"] = Field(
        "pld", description="Which driver this chamber node runs. 'legacy-mi' = --use-legacy-mi-mode, "
                           "and every pld op is refused")
    address: str = ""
    connected: bool = False
    backend: str = ""
    controller: str = ""
    holds_control: bool = Field(False, description="This node (client 'lumi-pascal') holds control")
    read_only: bool = False
    laser_control: bool | None = Field(
        None, description="excimer.control: the laser takes settings from the PLD PC (CM=2). False = the "
                          "second PC has them and every laser op is refused; None = no laser link")
    log_file: str | None = Field(None, description="The growth-log CSV this node is writing")


class PldState(PldReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the driver's readout.
    pass


# --- control -----------------------------------------------------------------------


class PldControlRequest(BaseModel):
    force: bool = Field(
        False, description="Take control from another client. Explicit and logged by the service; "
                           "never needed unless someone else holds it")


class PldControlReply(BaseModel):
    granted: bool
    controller: str = ""
    message: str = ""


# --- operations --------------------------------------------------------------------


class PldReply(BaseModel):
    ok: bool = Field(description="False = refused; `message` says why")
    message: str = ""


class PldValveRequest(BaseModel):
    valve: str = Field(description="Reference name, e.g. MV10, CV301")
    open: bool
    wait: bool = Field(True, description="Return once the status bit confirms")


class PldTargetRequest(BaseModel):
    target: str = Field(description="A..F or Clear")
    wait: bool = True


class PldPositionRequest(BaseModel):
    position: float = Field(description="mm for the mask and RHEED gun, degrees 0..360 for the sample")
    wait: bool = True


class PldAxisRequest(BaseModel):
    axis: Literal["TG", "Mask1", "RHEED gun X"]
    wait: bool = True


class PldSwitchRequest(BaseModel):
    on: bool


class PldCurrentRequest(BaseModel):
    amps: float


class PldCurrentLimitsRequest(BaseModel):
    min_amps: float
    max_amps: float


class PldTemperatureRequest(BaseModel):
    celsius: float
    ramp_c_per_min: float = Field(0.0, description="0 = jump")
    wait: bool = Field(False, description="Return once within `tolerance`")
    tolerance: float = Field(0.0, description="C; 0 = the service default, 5 C")


class PldShutterRequest(BaseModel):
    in_front: bool = Field(description="True = in front of the sample holder (closed)")


class PldAutoMotionRequest(BaseModel):
    spin: bool = True
    twist: bool = False


class PldFireRequest(BaseModel):
    pulses: int = Field(ge=1)
    rate_hz: float = Field(gt=0)
    wait: bool = True


class PldFlowRequest(BaseModel):
    mfc: int = Field(1, description="1 (MFC2 is not used in this chamber)")
    sccm: float


class PldFlowLimitsRequest(BaseModel):
    min_sccm: float
    max_sccm: float


class PldPressureRequest(BaseModel):
    torr: float
    gauge: Literal["", "IG1", "CDG10", "CDG11"] = Field("", description="Empty keeps the present choice")
    wait: bool = False
    tolerance: float = Field(0.0, description="Fraction; 0 = the service default, 0.1")


class PldPumpRequest(BaseModel):
    pump: Literal["TMP1", "TMP20", "TMP21", "TMP3", "DP1", "DP2", "DP3"]
    on: bool


class PldIonGaugeRequest(BaseModel):
    gauge: Literal["IG1", "IG3"]
    on: bool


class PldLaserOnRequest(BaseModel):
    wait: bool = Field(True, description="Return once OPMODE is ON (up to 20 s)")


class PldLaserModeRequest(BaseModel):
    mode: Literal["hv", "energy"]


class PldLaserHvRequest(BaseModel):
    kv: float


class PldRheedFilamentOnRequest(BaseModel):
    ma: float | None = Field(None, description="Filament set point, mA; None = the remembered one")


class PldRheedHvOnRequest(BaseModel):
    kv: float | None = Field(None, description="HV set point, kV; None = the remembered one")


class PldRheedFilamentRequest(BaseModel):
    ma: float


class PldRheedHvRequest(BaseModel):
    kv: float


class PldRheedFocusRequest(BaseModel):
    ma: float


class PldRheedDeflectionRequest(BaseModel):
    x_ma: float
    y_ma: float


class PldLaserEnergyRequest(BaseModel):
    mj: float


class PldLoggingRequest(BaseModel):
    file_name: str | None = Field(None, description="CSV name in the log folder; default chamber_log_<time>.csv")
    interval_s: float = Field(1.0, gt=0, description="Seconds between rows")
