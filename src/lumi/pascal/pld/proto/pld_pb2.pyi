from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Backend(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    BACKEND_UNSPECIFIED: _ClassVar[Backend]
    TWIN: _ClassVar[Backend]
    HARDWARE: _ClassVar[Backend]

class AxisStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    AXIS_STATUS_UNSPECIFIED: _ClassVar[AxisStatus]
    IDLE: _ClassVar[AxisStatus]
    MOVING: _ClassVar[AxisStatus]
    HOMING: _ClassVar[AxisStatus]
    SETTLING: _ClassVar[AxisStatus]
BACKEND_UNSPECIFIED: Backend
TWIN: Backend
HARDWARE: Backend
AXIS_STATUS_UNSPECIFIED: AxisStatus
IDLE: AxisStatus
MOVING: AxisStatus
HOMING: AxisStatus
SETTLING: AxisStatus

class Empty(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class WatchRequest(_message.Message):
    __slots__ = ("rate_hz",)
    RATE_HZ_FIELD_NUMBER: _ClassVar[int]
    rate_hz: float
    def __init__(self, rate_hz: _Optional[float] = ...) -> None: ...

class ChamberState(_message.Message):
    __slots__ = ("time", "backend", "connected", "controller", "valves", "pressures", "heater", "motion", "laser", "gas", "sample_shutter_away", "interlocks", "raw_words", "excimer", "read_only", "pumps", "load_lock", "session", "limits", "rheed")
    class ValvesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: bool
        def __init__(self, key: _Optional[str] = ..., value: _Optional[bool] = ...) -> None: ...
    class RawWordsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: int
        value: int
        def __init__(self, key: _Optional[int] = ..., value: _Optional[int] = ...) -> None: ...
    class PumpsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: Pump
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[Pump, _Mapping]] = ...) -> None: ...
    TIME_FIELD_NUMBER: _ClassVar[int]
    BACKEND_FIELD_NUMBER: _ClassVar[int]
    CONNECTED_FIELD_NUMBER: _ClassVar[int]
    CONTROLLER_FIELD_NUMBER: _ClassVar[int]
    VALVES_FIELD_NUMBER: _ClassVar[int]
    PRESSURES_FIELD_NUMBER: _ClassVar[int]
    HEATER_FIELD_NUMBER: _ClassVar[int]
    MOTION_FIELD_NUMBER: _ClassVar[int]
    LASER_FIELD_NUMBER: _ClassVar[int]
    GAS_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_SHUTTER_AWAY_FIELD_NUMBER: _ClassVar[int]
    INTERLOCKS_FIELD_NUMBER: _ClassVar[int]
    RAW_WORDS_FIELD_NUMBER: _ClassVar[int]
    EXCIMER_FIELD_NUMBER: _ClassVar[int]
    READ_ONLY_FIELD_NUMBER: _ClassVar[int]
    PUMPS_FIELD_NUMBER: _ClassVar[int]
    LOAD_LOCK_FIELD_NUMBER: _ClassVar[int]
    SESSION_FIELD_NUMBER: _ClassVar[int]
    LIMITS_FIELD_NUMBER: _ClassVar[int]
    RHEED_FIELD_NUMBER: _ClassVar[int]
    time: float
    backend: Backend
    connected: bool
    controller: str
    valves: _containers.ScalarMap[str, bool]
    pressures: Pressures
    heater: Heater
    motion: Motion
    laser: Laser
    gas: Gas
    sample_shutter_away: bool
    interlocks: _containers.RepeatedScalarFieldContainer[str]
    raw_words: _containers.ScalarMap[int, int]
    excimer: Excimer
    read_only: bool
    pumps: _containers.MessageMap[str, Pump]
    load_lock: LoadLock
    session: Session
    limits: Limits
    rheed: Rheed
    def __init__(self, time: _Optional[float] = ..., backend: _Optional[_Union[Backend, str]] = ..., connected: _Optional[bool] = ..., controller: _Optional[str] = ..., valves: _Optional[_Mapping[str, bool]] = ..., pressures: _Optional[_Union[Pressures, _Mapping]] = ..., heater: _Optional[_Union[Heater, _Mapping]] = ..., motion: _Optional[_Union[Motion, _Mapping]] = ..., laser: _Optional[_Union[Laser, _Mapping]] = ..., gas: _Optional[_Union[Gas, _Mapping]] = ..., sample_shutter_away: _Optional[bool] = ..., interlocks: _Optional[_Iterable[str]] = ..., raw_words: _Optional[_Mapping[int, int]] = ..., excimer: _Optional[_Union[Excimer, _Mapping]] = ..., read_only: _Optional[bool] = ..., pumps: _Optional[_Mapping[str, Pump]] = ..., load_lock: _Optional[_Union[LoadLock, _Mapping]] = ..., session: _Optional[_Union[Session, _Mapping]] = ..., limits: _Optional[_Union[Limits, _Mapping]] = ..., rheed: _Optional[_Union[Rheed, _Mapping]] = ...) -> None: ...

class Rheed(_message.Message):
    __slots__ = ("present", "read_only", "link_ok", "interlock", "filament_open", "filament_on", "filament_set_ma", "filament_target_ma", "filament_readback_ma", "filament_last_ma", "hv_on", "hv_set_kv", "hv_target_kv", "hv_readback_kv", "hv_last_kv", "emission_ua", "focus_on", "focus_set_ma", "focus_readback_ma", "x_on", "x_set_ma", "x_readback_ma", "y_on", "y_set_ma", "y_readback_ma", "hv_ramping_down", "filament_ramping_down", "last_alert", "hv_max_kv", "filament_min_ma", "filament_max_ma", "focus_min_ma", "focus_max_ma", "x_min_ma", "x_max_ma", "y_min_ma", "y_max_ma")
    PRESENT_FIELD_NUMBER: _ClassVar[int]
    READ_ONLY_FIELD_NUMBER: _ClassVar[int]
    LINK_OK_FIELD_NUMBER: _ClassVar[int]
    INTERLOCK_FIELD_NUMBER: _ClassVar[int]
    FILAMENT_OPEN_FIELD_NUMBER: _ClassVar[int]
    FILAMENT_ON_FIELD_NUMBER: _ClassVar[int]
    FILAMENT_SET_MA_FIELD_NUMBER: _ClassVar[int]
    FILAMENT_TARGET_MA_FIELD_NUMBER: _ClassVar[int]
    FILAMENT_READBACK_MA_FIELD_NUMBER: _ClassVar[int]
    FILAMENT_LAST_MA_FIELD_NUMBER: _ClassVar[int]
    HV_ON_FIELD_NUMBER: _ClassVar[int]
    HV_SET_KV_FIELD_NUMBER: _ClassVar[int]
    HV_TARGET_KV_FIELD_NUMBER: _ClassVar[int]
    HV_READBACK_KV_FIELD_NUMBER: _ClassVar[int]
    HV_LAST_KV_FIELD_NUMBER: _ClassVar[int]
    EMISSION_UA_FIELD_NUMBER: _ClassVar[int]
    FOCUS_ON_FIELD_NUMBER: _ClassVar[int]
    FOCUS_SET_MA_FIELD_NUMBER: _ClassVar[int]
    FOCUS_READBACK_MA_FIELD_NUMBER: _ClassVar[int]
    X_ON_FIELD_NUMBER: _ClassVar[int]
    X_SET_MA_FIELD_NUMBER: _ClassVar[int]
    X_READBACK_MA_FIELD_NUMBER: _ClassVar[int]
    Y_ON_FIELD_NUMBER: _ClassVar[int]
    Y_SET_MA_FIELD_NUMBER: _ClassVar[int]
    Y_READBACK_MA_FIELD_NUMBER: _ClassVar[int]
    HV_RAMPING_DOWN_FIELD_NUMBER: _ClassVar[int]
    FILAMENT_RAMPING_DOWN_FIELD_NUMBER: _ClassVar[int]
    LAST_ALERT_FIELD_NUMBER: _ClassVar[int]
    HV_MAX_KV_FIELD_NUMBER: _ClassVar[int]
    FILAMENT_MIN_MA_FIELD_NUMBER: _ClassVar[int]
    FILAMENT_MAX_MA_FIELD_NUMBER: _ClassVar[int]
    FOCUS_MIN_MA_FIELD_NUMBER: _ClassVar[int]
    FOCUS_MAX_MA_FIELD_NUMBER: _ClassVar[int]
    X_MIN_MA_FIELD_NUMBER: _ClassVar[int]
    X_MAX_MA_FIELD_NUMBER: _ClassVar[int]
    Y_MIN_MA_FIELD_NUMBER: _ClassVar[int]
    Y_MAX_MA_FIELD_NUMBER: _ClassVar[int]
    present: bool
    read_only: bool
    link_ok: bool
    interlock: bool
    filament_open: bool
    filament_on: bool
    filament_set_ma: float
    filament_target_ma: float
    filament_readback_ma: float
    filament_last_ma: float
    hv_on: bool
    hv_set_kv: float
    hv_target_kv: float
    hv_readback_kv: float
    hv_last_kv: float
    emission_ua: float
    focus_on: bool
    focus_set_ma: float
    focus_readback_ma: float
    x_on: bool
    x_set_ma: float
    x_readback_ma: float
    y_on: bool
    y_set_ma: float
    y_readback_ma: float
    hv_ramping_down: bool
    filament_ramping_down: bool
    last_alert: str
    hv_max_kv: float
    filament_min_ma: float
    filament_max_ma: float
    focus_min_ma: float
    focus_max_ma: float
    x_min_ma: float
    x_max_ma: float
    y_min_ma: float
    y_max_ma: float
    def __init__(self, present: _Optional[bool] = ..., read_only: _Optional[bool] = ..., link_ok: _Optional[bool] = ..., interlock: _Optional[bool] = ..., filament_open: _Optional[bool] = ..., filament_on: _Optional[bool] = ..., filament_set_ma: _Optional[float] = ..., filament_target_ma: _Optional[float] = ..., filament_readback_ma: _Optional[float] = ..., filament_last_ma: _Optional[float] = ..., hv_on: _Optional[bool] = ..., hv_set_kv: _Optional[float] = ..., hv_target_kv: _Optional[float] = ..., hv_readback_kv: _Optional[float] = ..., hv_last_kv: _Optional[float] = ..., emission_ua: _Optional[float] = ..., focus_on: _Optional[bool] = ..., focus_set_ma: _Optional[float] = ..., focus_readback_ma: _Optional[float] = ..., x_on: _Optional[bool] = ..., x_set_ma: _Optional[float] = ..., x_readback_ma: _Optional[float] = ..., y_on: _Optional[bool] = ..., y_set_ma: _Optional[float] = ..., y_readback_ma: _Optional[float] = ..., hv_ramping_down: _Optional[bool] = ..., filament_ramping_down: _Optional[bool] = ..., last_alert: _Optional[str] = ..., hv_max_kv: _Optional[float] = ..., filament_min_ma: _Optional[float] = ..., filament_max_ma: _Optional[float] = ..., focus_min_ma: _Optional[float] = ..., focus_max_ma: _Optional[float] = ..., x_min_ma: _Optional[float] = ..., x_max_ma: _Optional[float] = ..., y_min_ma: _Optional[float] = ..., y_max_ma: _Optional[float] = ...) -> None: ...

class Session(_message.Message):
    __slots__ = ("plc_session", "watchdog", "link_error")
    PLC_SESSION_FIELD_NUMBER: _ClassVar[int]
    WATCHDOG_FIELD_NUMBER: _ClassVar[int]
    LINK_ERROR_FIELD_NUMBER: _ClassVar[int]
    plc_session: bool
    watchdog: bool
    link_error: str
    def __init__(self, plc_session: _Optional[bool] = ..., watchdog: _Optional[bool] = ..., link_error: _Optional[str] = ...) -> None: ...

class Limits(_message.Message):
    __slots__ = ("ig1_max_torr", "ig3_max_torr", "mv3_max_torr", "heater_max_celsius", "heater_max_amps")
    IG1_MAX_TORR_FIELD_NUMBER: _ClassVar[int]
    IG3_MAX_TORR_FIELD_NUMBER: _ClassVar[int]
    MV3_MAX_TORR_FIELD_NUMBER: _ClassVar[int]
    HEATER_MAX_CELSIUS_FIELD_NUMBER: _ClassVar[int]
    HEATER_MAX_AMPS_FIELD_NUMBER: _ClassVar[int]
    ig1_max_torr: float
    ig3_max_torr: float
    mv3_max_torr: float
    heater_max_celsius: float
    heater_max_amps: float
    def __init__(self, ig1_max_torr: _Optional[float] = ..., ig3_max_torr: _Optional[float] = ..., mv3_max_torr: _Optional[float] = ..., heater_max_celsius: _Optional[float] = ..., heater_max_amps: _Optional[float] = ...) -> None: ...

class LoadLock(_message.Message):
    __slots__ = ("hatch_closed", "ready", "sequence", "step", "pump_down_complete", "below_sp1", "below_sp2", "ig3_disabled", "by_service", "gv1")
    HATCH_CLOSED_FIELD_NUMBER: _ClassVar[int]
    READY_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    STEP_FIELD_NUMBER: _ClassVar[int]
    PUMP_DOWN_COMPLETE_FIELD_NUMBER: _ClassVar[int]
    BELOW_SP1_FIELD_NUMBER: _ClassVar[int]
    BELOW_SP2_FIELD_NUMBER: _ClassVar[int]
    IG3_DISABLED_FIELD_NUMBER: _ClassVar[int]
    BY_SERVICE_FIELD_NUMBER: _ClassVar[int]
    GV1_FIELD_NUMBER: _ClassVar[int]
    hatch_closed: bool
    ready: bool
    sequence: str
    step: str
    pump_down_complete: bool
    below_sp1: bool
    below_sp2: bool
    ig3_disabled: bool
    by_service: bool
    gv1: str
    def __init__(self, hatch_closed: _Optional[bool] = ..., ready: _Optional[bool] = ..., sequence: _Optional[str] = ..., step: _Optional[str] = ..., pump_down_complete: _Optional[bool] = ..., below_sp1: _Optional[bool] = ..., below_sp2: _Optional[bool] = ..., ig3_disabled: _Optional[bool] = ..., by_service: _Optional[bool] = ..., gv1: _Optional[str] = ...) -> None: ...

class Pressures(_message.Message):
    __slots__ = ("ig1", "ig3", "cdg10", "cdg11", "tcg", "tcg31", "ig1_on", "ig3_on")
    IG1_FIELD_NUMBER: _ClassVar[int]
    IG3_FIELD_NUMBER: _ClassVar[int]
    CDG10_FIELD_NUMBER: _ClassVar[int]
    CDG11_FIELD_NUMBER: _ClassVar[int]
    TCG_FIELD_NUMBER: _ClassVar[int]
    TCG31_FIELD_NUMBER: _ClassVar[int]
    IG1_ON_FIELD_NUMBER: _ClassVar[int]
    IG3_ON_FIELD_NUMBER: _ClassVar[int]
    ig1: float
    ig3: float
    cdg10: float
    cdg11: float
    tcg: _containers.RepeatedScalarFieldContainer[float]
    tcg31: float
    ig1_on: bool
    ig3_on: bool
    def __init__(self, ig1: _Optional[float] = ..., ig3: _Optional[float] = ..., cdg10: _Optional[float] = ..., cdg11: _Optional[float] = ..., tcg: _Optional[_Iterable[float]] = ..., tcg31: _Optional[float] = ..., ig1_on: _Optional[bool] = ..., ig3_on: _Optional[bool] = ...) -> None: ...

class Heater(_message.Message):
    __slots__ = ("temperature", "emissivity", "current_set", "current_monitor", "power_on", "ready", "started", "temperature_setpoint", "pid_on", "key_on", "start_on", "threshold_on", "current_min", "current_max", "temperature_goal", "ramp_c_per_min", "auto_tune", "kp", "ki", "kd", "writes_failing", "tune_cycles_done", "tune_cycles", "warming_up")
    TEMPERATURE_FIELD_NUMBER: _ClassVar[int]
    EMISSIVITY_FIELD_NUMBER: _ClassVar[int]
    CURRENT_SET_FIELD_NUMBER: _ClassVar[int]
    CURRENT_MONITOR_FIELD_NUMBER: _ClassVar[int]
    POWER_ON_FIELD_NUMBER: _ClassVar[int]
    READY_FIELD_NUMBER: _ClassVar[int]
    STARTED_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_SETPOINT_FIELD_NUMBER: _ClassVar[int]
    PID_ON_FIELD_NUMBER: _ClassVar[int]
    KEY_ON_FIELD_NUMBER: _ClassVar[int]
    START_ON_FIELD_NUMBER: _ClassVar[int]
    THRESHOLD_ON_FIELD_NUMBER: _ClassVar[int]
    CURRENT_MIN_FIELD_NUMBER: _ClassVar[int]
    CURRENT_MAX_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_GOAL_FIELD_NUMBER: _ClassVar[int]
    RAMP_C_PER_MIN_FIELD_NUMBER: _ClassVar[int]
    AUTO_TUNE_FIELD_NUMBER: _ClassVar[int]
    KP_FIELD_NUMBER: _ClassVar[int]
    KI_FIELD_NUMBER: _ClassVar[int]
    KD_FIELD_NUMBER: _ClassVar[int]
    WRITES_FAILING_FIELD_NUMBER: _ClassVar[int]
    TUNE_CYCLES_DONE_FIELD_NUMBER: _ClassVar[int]
    TUNE_CYCLES_FIELD_NUMBER: _ClassVar[int]
    WARMING_UP_FIELD_NUMBER: _ClassVar[int]
    temperature: float
    emissivity: float
    current_set: float
    current_monitor: float
    power_on: bool
    ready: bool
    started: bool
    temperature_setpoint: float
    pid_on: bool
    key_on: bool
    start_on: bool
    threshold_on: bool
    current_min: float
    current_max: float
    temperature_goal: float
    ramp_c_per_min: float
    auto_tune: bool
    kp: float
    ki: float
    kd: float
    writes_failing: bool
    tune_cycles_done: int
    tune_cycles: int
    warming_up: bool
    def __init__(self, temperature: _Optional[float] = ..., emissivity: _Optional[float] = ..., current_set: _Optional[float] = ..., current_monitor: _Optional[float] = ..., power_on: _Optional[bool] = ..., ready: _Optional[bool] = ..., started: _Optional[bool] = ..., temperature_setpoint: _Optional[float] = ..., pid_on: _Optional[bool] = ..., key_on: _Optional[bool] = ..., start_on: _Optional[bool] = ..., threshold_on: _Optional[bool] = ..., current_min: _Optional[float] = ..., current_max: _Optional[float] = ..., temperature_goal: _Optional[float] = ..., ramp_c_per_min: _Optional[float] = ..., auto_tune: _Optional[bool] = ..., kp: _Optional[float] = ..., ki: _Optional[float] = ..., kd: _Optional[float] = ..., writes_failing: _Optional[bool] = ..., tune_cycles_done: _Optional[int] = ..., tune_cycles: _Optional[int] = ..., warming_up: _Optional[bool] = ...) -> None: ...

class Motion(_message.Message):
    __slots__ = ("target_angle", "target", "sample_angle", "mask_position", "rheed_gun_x", "axes", "target_spinning", "target_twisting", "motor_free", "auto_spin", "auto_twist")
    class AxesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: AxisStatus
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[AxisStatus, str]] = ...) -> None: ...
    TARGET_ANGLE_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_ANGLE_FIELD_NUMBER: _ClassVar[int]
    MASK_POSITION_FIELD_NUMBER: _ClassVar[int]
    RHEED_GUN_X_FIELD_NUMBER: _ClassVar[int]
    AXES_FIELD_NUMBER: _ClassVar[int]
    TARGET_SPINNING_FIELD_NUMBER: _ClassVar[int]
    TARGET_TWISTING_FIELD_NUMBER: _ClassVar[int]
    MOTOR_FREE_FIELD_NUMBER: _ClassVar[int]
    AUTO_SPIN_FIELD_NUMBER: _ClassVar[int]
    AUTO_TWIST_FIELD_NUMBER: _ClassVar[int]
    target_angle: float
    target: str
    sample_angle: float
    mask_position: float
    rheed_gun_x: float
    axes: _containers.ScalarMap[str, AxisStatus]
    target_spinning: bool
    target_twisting: bool
    motor_free: bool
    auto_spin: bool
    auto_twist: bool
    def __init__(self, target_angle: _Optional[float] = ..., target: _Optional[str] = ..., sample_angle: _Optional[float] = ..., mask_position: _Optional[float] = ..., rheed_gun_x: _Optional[float] = ..., axes: _Optional[_Mapping[str, AxisStatus]] = ..., target_spinning: _Optional[bool] = ..., target_twisting: _Optional[bool] = ..., motor_free: _Optional[bool] = ..., auto_spin: _Optional[bool] = ..., auto_twist: _Optional[bool] = ...) -> None: ...

class Pump(_message.Message):
    __slots__ = ("running", "at_speed", "state")
    RUNNING_FIELD_NUMBER: _ClassVar[int]
    AT_SPEED_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    running: bool
    at_speed: bool
    state: str
    def __init__(self, running: _Optional[bool] = ..., at_speed: _Optional[bool] = ..., state: _Optional[str] = ...) -> None: ...

class Laser(_message.Message):
    __slots__ = ("status", "pulses_fired", "missed_shots", "rate_hz", "pulses_requested", "stop_requested")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    PULSES_FIRED_FIELD_NUMBER: _ClassVar[int]
    MISSED_SHOTS_FIELD_NUMBER: _ClassVar[int]
    RATE_HZ_FIELD_NUMBER: _ClassVar[int]
    PULSES_REQUESTED_FIELD_NUMBER: _ClassVar[int]
    STOP_REQUESTED_FIELD_NUMBER: _ClassVar[int]
    status: str
    pulses_fired: int
    missed_shots: int
    rate_hz: float
    pulses_requested: int
    stop_requested: bool
    def __init__(self, status: _Optional[str] = ..., pulses_fired: _Optional[int] = ..., missed_shots: _Optional[int] = ..., rate_hz: _Optional[float] = ..., pulses_requested: _Optional[int] = ..., stop_requested: _Optional[bool] = ...) -> None: ...

class Excimer(_message.Message):
    __slots__ = ("present", "connected", "opmode", "waiting", "status", "codes", "mode", "hv", "energy_set", "energy", "control", "trigger", "reprate", "counter", "tube_temperature", "tube_pressure", "version", "hv_min", "hv_max", "energy_min", "energy_max")
    PRESENT_FIELD_NUMBER: _ClassVar[int]
    CONNECTED_FIELD_NUMBER: _ClassVar[int]
    OPMODE_FIELD_NUMBER: _ClassVar[int]
    WAITING_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CODES_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    HV_FIELD_NUMBER: _ClassVar[int]
    ENERGY_SET_FIELD_NUMBER: _ClassVar[int]
    ENERGY_FIELD_NUMBER: _ClassVar[int]
    CONTROL_FIELD_NUMBER: _ClassVar[int]
    TRIGGER_FIELD_NUMBER: _ClassVar[int]
    REPRATE_FIELD_NUMBER: _ClassVar[int]
    COUNTER_FIELD_NUMBER: _ClassVar[int]
    TUBE_TEMPERATURE_FIELD_NUMBER: _ClassVar[int]
    TUBE_PRESSURE_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    HV_MIN_FIELD_NUMBER: _ClassVar[int]
    HV_MAX_FIELD_NUMBER: _ClassVar[int]
    ENERGY_MIN_FIELD_NUMBER: _ClassVar[int]
    ENERGY_MAX_FIELD_NUMBER: _ClassVar[int]
    present: bool
    connected: bool
    opmode: str
    waiting: bool
    status: str
    codes: _containers.RepeatedCompositeFieldContainer[LaserCode]
    mode: str
    hv: float
    energy_set: float
    energy: float
    control: bool
    trigger: str
    reprate: float
    counter: int
    tube_temperature: float
    tube_pressure: float
    version: str
    hv_min: float
    hv_max: float
    energy_min: float
    energy_max: float
    def __init__(self, present: _Optional[bool] = ..., connected: _Optional[bool] = ..., opmode: _Optional[str] = ..., waiting: _Optional[bool] = ..., status: _Optional[str] = ..., codes: _Optional[_Iterable[_Union[LaserCode, _Mapping]]] = ..., mode: _Optional[str] = ..., hv: _Optional[float] = ..., energy_set: _Optional[float] = ..., energy: _Optional[float] = ..., control: _Optional[bool] = ..., trigger: _Optional[str] = ..., reprate: _Optional[float] = ..., counter: _Optional[int] = ..., tube_temperature: _Optional[float] = ..., tube_pressure: _Optional[float] = ..., version: _Optional[str] = ..., hv_min: _Optional[float] = ..., hv_max: _Optional[float] = ..., energy_min: _Optional[float] = ..., energy_max: _Optional[float] = ...) -> None: ...

class LaserCode(_message.Message):
    __slots__ = ("code", "kind", "meaning")
    CODE_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    MEANING_FIELD_NUMBER: _ClassVar[int]
    code: int
    kind: str
    meaning: str
    def __init__(self, code: _Optional[int] = ..., kind: _Optional[str] = ..., meaning: _Optional[str] = ...) -> None: ...

class Gas(_message.Message):
    __slots__ = ("mfc1_setpoint", "mfc1_flow", "pumping", "pressure_control", "pressure_setpoint", "pressure_gauge", "pressure_mode", "flow_min", "flow_max", "pressure_kp", "pressure_ki", "pressure_kd", "pressure_table", "pressure_switch_step")
    MFC1_SETPOINT_FIELD_NUMBER: _ClassVar[int]
    MFC1_FLOW_FIELD_NUMBER: _ClassVar[int]
    PUMPING_FIELD_NUMBER: _ClassVar[int]
    PRESSURE_CONTROL_FIELD_NUMBER: _ClassVar[int]
    PRESSURE_SETPOINT_FIELD_NUMBER: _ClassVar[int]
    PRESSURE_GAUGE_FIELD_NUMBER: _ClassVar[int]
    PRESSURE_MODE_FIELD_NUMBER: _ClassVar[int]
    FLOW_MIN_FIELD_NUMBER: _ClassVar[int]
    FLOW_MAX_FIELD_NUMBER: _ClassVar[int]
    PRESSURE_KP_FIELD_NUMBER: _ClassVar[int]
    PRESSURE_KI_FIELD_NUMBER: _ClassVar[int]
    PRESSURE_KD_FIELD_NUMBER: _ClassVar[int]
    PRESSURE_TABLE_FIELD_NUMBER: _ClassVar[int]
    PRESSURE_SWITCH_STEP_FIELD_NUMBER: _ClassVar[int]
    mfc1_setpoint: float
    mfc1_flow: float
    pumping: str
    pressure_control: bool
    pressure_setpoint: float
    pressure_gauge: str
    pressure_mode: str
    flow_min: float
    flow_max: float
    pressure_kp: float
    pressure_ki: float
    pressure_kd: float
    pressure_table: _containers.RepeatedCompositeFieldContainer[FlowPoint]
    pressure_switch_step: str
    def __init__(self, mfc1_setpoint: _Optional[float] = ..., mfc1_flow: _Optional[float] = ..., pumping: _Optional[str] = ..., pressure_control: _Optional[bool] = ..., pressure_setpoint: _Optional[float] = ..., pressure_gauge: _Optional[str] = ..., pressure_mode: _Optional[str] = ..., flow_min: _Optional[float] = ..., flow_max: _Optional[float] = ..., pressure_kp: _Optional[float] = ..., pressure_ki: _Optional[float] = ..., pressure_kd: _Optional[float] = ..., pressure_table: _Optional[_Iterable[_Union[FlowPoint, _Mapping]]] = ..., pressure_switch_step: _Optional[str] = ...) -> None: ...

class FlowPoint(_message.Message):
    __slots__ = ("sccm", "torr")
    SCCM_FIELD_NUMBER: _ClassVar[int]
    TORR_FIELD_NUMBER: _ClassVar[int]
    sccm: float
    torr: float
    def __init__(self, sccm: _Optional[float] = ..., torr: _Optional[float] = ...) -> None: ...

class Event(_message.Message):
    __slots__ = ("time", "kind", "message", "detail")
    class DetailEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    TIME_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    time: float
    kind: str
    message: str
    detail: _containers.ScalarMap[str, str]
    def __init__(self, time: _Optional[float] = ..., kind: _Optional[str] = ..., message: _Optional[str] = ..., detail: _Optional[_Mapping[str, str]] = ...) -> None: ...

class ControlRequest(_message.Message):
    __slots__ = ("client", "force")
    CLIENT_FIELD_NUMBER: _ClassVar[int]
    FORCE_FIELD_NUMBER: _ClassVar[int]
    client: str
    force: bool
    def __init__(self, client: _Optional[str] = ..., force: _Optional[bool] = ...) -> None: ...

class ControlReply(_message.Message):
    __slots__ = ("granted", "controller", "message")
    GRANTED_FIELD_NUMBER: _ClassVar[int]
    CONTROLLER_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    granted: bool
    controller: str
    message: str
    def __init__(self, granted: _Optional[bool] = ..., controller: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class OperationReply(_message.Message):
    __slots__ = ("ok", "message")
    OK_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    message: str
    def __init__(self, ok: _Optional[bool] = ..., message: _Optional[str] = ...) -> None: ...

class ValveRequest(_message.Message):
    __slots__ = ("valve", "open", "wait")
    VALVE_FIELD_NUMBER: _ClassVar[int]
    OPEN_FIELD_NUMBER: _ClassVar[int]
    WAIT_FIELD_NUMBER: _ClassVar[int]
    valve: str
    open: bool
    wait: bool
    def __init__(self, valve: _Optional[str] = ..., open: _Optional[bool] = ..., wait: _Optional[bool] = ...) -> None: ...

class TargetRequest(_message.Message):
    __slots__ = ("target", "wait")
    TARGET_FIELD_NUMBER: _ClassVar[int]
    WAIT_FIELD_NUMBER: _ClassVar[int]
    target: str
    wait: bool
    def __init__(self, target: _Optional[str] = ..., wait: _Optional[bool] = ...) -> None: ...

class PositionRequest(_message.Message):
    __slots__ = ("position", "wait")
    POSITION_FIELD_NUMBER: _ClassVar[int]
    WAIT_FIELD_NUMBER: _ClassVar[int]
    position: float
    wait: bool
    def __init__(self, position: _Optional[float] = ..., wait: _Optional[bool] = ...) -> None: ...

class AxisRequest(_message.Message):
    __slots__ = ("axis", "wait")
    AXIS_FIELD_NUMBER: _ClassVar[int]
    WAIT_FIELD_NUMBER: _ClassVar[int]
    axis: str
    wait: bool
    def __init__(self, axis: _Optional[str] = ..., wait: _Optional[bool] = ...) -> None: ...

class SwitchRequest(_message.Message):
    __slots__ = ("on",)
    ON_FIELD_NUMBER: _ClassVar[int]
    on: bool
    def __init__(self, on: _Optional[bool] = ...) -> None: ...

class CurrentRequest(_message.Message):
    __slots__ = ("amps",)
    AMPS_FIELD_NUMBER: _ClassVar[int]
    amps: float
    def __init__(self, amps: _Optional[float] = ...) -> None: ...

class FlowLimitsRequest(_message.Message):
    __slots__ = ("min_sccm", "max_sccm")
    MIN_SCCM_FIELD_NUMBER: _ClassVar[int]
    MAX_SCCM_FIELD_NUMBER: _ClassVar[int]
    min_sccm: float
    max_sccm: float
    def __init__(self, min_sccm: _Optional[float] = ..., max_sccm: _Optional[float] = ...) -> None: ...

class CurrentLimitsRequest(_message.Message):
    __slots__ = ("min_amps", "max_amps")
    MIN_AMPS_FIELD_NUMBER: _ClassVar[int]
    MAX_AMPS_FIELD_NUMBER: _ClassVar[int]
    min_amps: float
    max_amps: float
    def __init__(self, min_amps: _Optional[float] = ..., max_amps: _Optional[float] = ...) -> None: ...

class TemperatureRequest(_message.Message):
    __slots__ = ("celsius", "ramp_c_per_min", "wait", "tolerance")
    CELSIUS_FIELD_NUMBER: _ClassVar[int]
    RAMP_C_PER_MIN_FIELD_NUMBER: _ClassVar[int]
    WAIT_FIELD_NUMBER: _ClassVar[int]
    TOLERANCE_FIELD_NUMBER: _ClassVar[int]
    celsius: float
    ramp_c_per_min: float
    wait: bool
    tolerance: float
    def __init__(self, celsius: _Optional[float] = ..., ramp_c_per_min: _Optional[float] = ..., wait: _Optional[bool] = ..., tolerance: _Optional[float] = ...) -> None: ...

class ShutterRequest(_message.Message):
    __slots__ = ("in_front",)
    IN_FRONT_FIELD_NUMBER: _ClassVar[int]
    in_front: bool
    def __init__(self, in_front: _Optional[bool] = ...) -> None: ...

class AutoMotionRequest(_message.Message):
    __slots__ = ("spin", "twist")
    SPIN_FIELD_NUMBER: _ClassVar[int]
    TWIST_FIELD_NUMBER: _ClassVar[int]
    spin: bool
    twist: bool
    def __init__(self, spin: _Optional[bool] = ..., twist: _Optional[bool] = ...) -> None: ...

class LaserRequest(_message.Message):
    __slots__ = ("pulses", "rate_hz", "wait")
    PULSES_FIELD_NUMBER: _ClassVar[int]
    RATE_HZ_FIELD_NUMBER: _ClassVar[int]
    WAIT_FIELD_NUMBER: _ClassVar[int]
    pulses: int
    rate_hz: float
    wait: bool
    def __init__(self, pulses: _Optional[int] = ..., rate_hz: _Optional[float] = ..., wait: _Optional[bool] = ...) -> None: ...

class LaserOnRequest(_message.Message):
    __slots__ = ("wait",)
    WAIT_FIELD_NUMBER: _ClassVar[int]
    wait: bool
    def __init__(self, wait: _Optional[bool] = ...) -> None: ...

class LaserModeRequest(_message.Message):
    __slots__ = ("mode",)
    MODE_FIELD_NUMBER: _ClassVar[int]
    mode: str
    def __init__(self, mode: _Optional[str] = ...) -> None: ...

class LaserHvRequest(_message.Message):
    __slots__ = ("kv",)
    KV_FIELD_NUMBER: _ClassVar[int]
    kv: float
    def __init__(self, kv: _Optional[float] = ...) -> None: ...

class LaserEnergyRequest(_message.Message):
    __slots__ = ("mj",)
    MJ_FIELD_NUMBER: _ClassVar[int]
    mj: float
    def __init__(self, mj: _Optional[float] = ...) -> None: ...

class RheedFilamentOnRequest(_message.Message):
    __slots__ = ("ma",)
    MA_FIELD_NUMBER: _ClassVar[int]
    ma: float
    def __init__(self, ma: _Optional[float] = ...) -> None: ...

class RheedHvOnRequest(_message.Message):
    __slots__ = ("kv",)
    KV_FIELD_NUMBER: _ClassVar[int]
    kv: float
    def __init__(self, kv: _Optional[float] = ...) -> None: ...

class RheedFilamentRequest(_message.Message):
    __slots__ = ("ma",)
    MA_FIELD_NUMBER: _ClassVar[int]
    ma: float
    def __init__(self, ma: _Optional[float] = ...) -> None: ...

class RheedHvRequest(_message.Message):
    __slots__ = ("kv",)
    KV_FIELD_NUMBER: _ClassVar[int]
    kv: float
    def __init__(self, kv: _Optional[float] = ...) -> None: ...

class RheedFocusRequest(_message.Message):
    __slots__ = ("ma",)
    MA_FIELD_NUMBER: _ClassVar[int]
    ma: float
    def __init__(self, ma: _Optional[float] = ...) -> None: ...

class RheedDeflectionRequest(_message.Message):
    __slots__ = ("x_ma", "y_ma")
    X_MA_FIELD_NUMBER: _ClassVar[int]
    Y_MA_FIELD_NUMBER: _ClassVar[int]
    x_ma: float
    y_ma: float
    def __init__(self, x_ma: _Optional[float] = ..., y_ma: _Optional[float] = ...) -> None: ...

class FlowRequest(_message.Message):
    __slots__ = ("mfc", "sccm")
    MFC_FIELD_NUMBER: _ClassVar[int]
    SCCM_FIELD_NUMBER: _ClassVar[int]
    mfc: int
    sccm: float
    def __init__(self, mfc: _Optional[int] = ..., sccm: _Optional[float] = ...) -> None: ...

class PressureRequest(_message.Message):
    __slots__ = ("torr", "gauge", "wait", "tolerance")
    TORR_FIELD_NUMBER: _ClassVar[int]
    GAUGE_FIELD_NUMBER: _ClassVar[int]
    WAIT_FIELD_NUMBER: _ClassVar[int]
    TOLERANCE_FIELD_NUMBER: _ClassVar[int]
    torr: float
    gauge: str
    wait: bool
    tolerance: float
    def __init__(self, torr: _Optional[float] = ..., gauge: _Optional[str] = ..., wait: _Optional[bool] = ..., tolerance: _Optional[float] = ...) -> None: ...

class IonGaugeRequest(_message.Message):
    __slots__ = ("gauge", "on")
    GAUGE_FIELD_NUMBER: _ClassVar[int]
    ON_FIELD_NUMBER: _ClassVar[int]
    gauge: str
    on: bool
    def __init__(self, gauge: _Optional[str] = ..., on: _Optional[bool] = ...) -> None: ...

class PumpRequest(_message.Message):
    __slots__ = ("pump", "on")
    PUMP_FIELD_NUMBER: _ClassVar[int]
    ON_FIELD_NUMBER: _ClassVar[int]
    pump: str
    on: bool
    def __init__(self, pump: _Optional[str] = ..., on: _Optional[bool] = ...) -> None: ...
