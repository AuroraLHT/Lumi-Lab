"""A stand-in pld-service for `--src sim`: the `pld.v1.Chamber` gRPC API over the
chamber simulator's `ChamberModel`.

The real service and its digital twin (`rea-twin` + `pld-service --twin`) live on the
PLD PC. This lets the new driver run end to end anywhere -- a laptop, CI, the server
host's demo stack -- through the same gRPC client, handler, growth-log writer and
LogReader as production, with only the far end of the channel swapped. It is served
on an ephemeral 127.0.0.1 port, in-process.

It is the Lumi simulator speaking the REA protocol, not the REA twin: the physics is
`lumi.pascal.sim.model`'s, and the service's own rules are reproduced only where the
driver depends on them --

- control ownership: operations need control (FAILED_PRECONDITION otherwise), and
  AcquireControl is refused while another client holds it, unless forced;
- `wait` blocks until the model gets there;
- pressure: the low/high regimes and the refused gap between them, with IG1 off and
  MV10 closed in the high regime;
- the pyrometer reads NaN below its range, as the real one does (the legacy simulator
  floors it at Pyro_min instead, which is PLD Controller's display);
- the excimer refuses every operation until TakeLaserControl (it starts at CM=1, the
  second PC), keeps its caps, and LaserOn runs the preflight, spends LASER_WARMUP_S in
  OFF,WAIT and with `wait` returns once ON (LASER_ON_TIMEOUT_S at most). An interlock is
  injected with `set_laser_interlock(code, meaning)`.

Everything else (valves beyond MV10, the excimer's tube) is bookkeeping, not physics.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent import futures

import grpc

from lumi.pascal.pld.proto import pld_pb2 as pb
from lumi.pascal.pld.proto import pld_pb2_grpc as pb_grpc
from lumi.pascal.sim.model import ChamberModel

log = logging.getLogger(__name__)

NAN = float("nan")
LOW_REGIME_MAX = 2e-4
HIGH_REGIME_MIN = 3e-3
HV_CAPS = (18.5, 25.5)
ENERGY_CAPS = (100.0, 400.0)
LASER_WARMUP_S = 8.0  # OFF,WAIT after OPMODE=ON
LASER_ON_TIMEOUT_S = 20.0


class ModelTicker(threading.Thread):
    """Integrates the model in (scaled) real time -- the role `SimLogWriter` plays for
    the legacy simulator, without the CSV: under this driver the Pascal node writes the
    log from the service's state, as it does in production."""

    def __init__(self, model: ChamberModel, *, time_scale: float = 1.0, tick_interval: float = 0.05) -> None:
        super().__init__(name="sim_model_ticker", daemon=True)
        self.model = model
        self.time_scale = max(time_scale, 1e-6)
        self.tick_interval = tick_interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        last = time.monotonic()
        while not self._stop_event.wait(self.tick_interval):
            now = time.monotonic()
            self.model.tick((now - last) * self.time_scale)
            last = now

    def stop(self) -> None:
        self._stop_event.set()


class SimChamberServicer(pb_grpc.ChamberServicer):
    def __init__(self, model: ChamberModel, *, time_scale: float = 1.0, command_timeout: float = 3600.0) -> None:
        self.model = model
        self.time_scale = max(time_scale, 1e-6)
        self.command_timeout = command_timeout
        self._lock = threading.RLock()
        self.controller = ""
        self.valves = {"MV10": True, "MV11": True, "FV1": True, "MV3": False, "FV3": True, "RV3": False}
        self.ig1_on = True
        self.ig3_on = True
        self.threshold_on = False
        self.auto_spin, self.auto_twist = True, False
        self.flow_limits = (0.0, 30.0)
        self.excimer = {"control": False, "opmode": "OFF", "mode": "HV PGR", "hv": 22.0, "energy_set": 200.0}
        self._laser_on_at: float | None = None  # monotonic time OFF,WAIT ends
        self.laser_interlock: tuple[int, str] | None = None
        self.read_only = False  # reported only, as hardware without --allow-operations would
        self._subscribers: list[queue.Queue] = []

    # --- helpers ------------------------------------------------------------------

    def _client(self, context) -> str:
        return dict(context.invocation_metadata()).get("x-client", "")

    def _require_control(self, context) -> None:
        client = self._client(context)
        if not client or client != self.controller:
            holder = f"control is held by {self.controller!r}" if self.controller else "nobody holds control"
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, f"{client or 'this client'} does not hold control ({holder})")

    def _emit(self, kind: str, message: str, **detail: str) -> None:
        event = pb.Event(time=time.time(), kind=kind, message=message, detail=detail)
        for q in list(self._subscribers):
            q.put(event)

    def _wait(self, predicate, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + (timeout if timeout is not None else self.command_timeout / self.time_scale)
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def _ok(self, message: str = "done") -> pb.OperationReply:
        return pb.OperationReply(ok=True, message=message)

    def _no(self, message: str) -> pb.OperationReply:
        return pb.OperationReply(ok=False, message=message)

    def _motion(self, request, context, start, what: str) -> pb.OperationReply:
        self._require_control(context)
        if self.model.motor_lock_released:
            return self._no("motor holding lock is released (motor free); engage it first")
        try:
            start()
        except KeyError as e:
            return self._no(str(e))
        if getattr(request, "wait", False):
            if not self._wait(lambda: self.model.motor_free):
                return self._no(f"{what}: did not arrive in time")
            self._emit("move_done", f"{what} arrived")
        return self._ok(what)

    # --- state --------------------------------------------------------------------

    def _state(self) -> pb.ChamberState:
        m, c = self.model, self.model.config
        with m._lock:
            pid = m.temperature_mode == "PID"
            temperature = m.temperature_true
            if c.temperature_noise:
                temperature += m._rng.gauss(0.0, c.temperature_noise)
            high = m.pressure_setpoint >= HIGH_REGIME_MIN
            p = m.true_pressure
            busy_target = m.rotation.busy
            firing = m._pulses_remaining > 0
            return pb.ChamberState(
                time=time.time(), backend=pb.TWIN, connected=True, controller=self.controller,
                valves=self.valves,
                pressures=pb.Pressures(
                    ig1=p if self.ig1_on else 0.0, ig3=c.load_lock_pressure,
                    cdg10=p + c.process_gauge_offset, cdg11=p + c.process_gauge_offset,
                    tcg=[c.backing_pressure] * 3, tcg31=c.backing_pressure,
                    ig1_on=self.ig1_on, ig3_on=self.ig3_on,
                ),
                heater=pb.Heater(
                    # The pyrometer has no reading below its range.
                    temperature=temperature if temperature >= c.pyro_min else NAN,
                    emissivity=0.7, current_set=m.current_command, current_monitor=m.current_moni,
                    power_on=m.heating_laser_on, ready=True, started=m.heating_laser_on,
                    temperature_setpoint=m.temperature_setpoint if pid else NAN, pid_on=pid,
                    key_on=not m.heating_locked, start_on=m.heating_laser_on, threshold_on=self.threshold_on,
                    current_min=m.min_current, current_max=m.max_current,
                    temperature_goal=m.temperature_target if pid else NAN,
                    ramp_c_per_min=m.ramp_rate if m.ramp_enabled else 0.0,
                ),
                motion=pb.Motion(
                    target_angle=m.rotation.position, target="" if busy_target else m.target_slot,
                    sample_angle=m.sample_rot.position, mask_position=m.mask1.position,
                    rheed_gun_x=m.rheed_gun_x.position,
                    axes={
                        "TG": pb.MOVING if m.rotation.busy else pb.IDLE,
                        "Mask1": pb.MOVING if m.mask1.busy else pb.IDLE,
                        "RHEED gun X": pb.MOVING if m.rheed_gun_x.busy else pb.IDLE,
                        "Substrate": pb.MOVING if m.sample_rot.busy else pb.IDLE,
                    },
                    target_spinning=m.target_rotation_mode == "ON" or (m.target_rotation_mode == "AUTO" and firing),
                    target_twisting=m.target_twist_mode == "ON" or (m.target_twist_mode == "AUTO" and firing),
                    motor_free=m.motor_lock_released, auto_spin=self.auto_spin, auto_twist=self.auto_twist,
                ),
                laser=pb.Laser(
                    status="firing" if firing else ("finished" if m.laser_pulses else "idle"),
                    pulses_fired=m.laser_pulses, missed_shots=m.missed_pulses,
                ),
                gas=pb.Gas(
                    mfc1_setpoint=m.mfc_set[1], mfc1_flow=m.mfc_moni[1],
                    pumping="throttled" if not self.valves.get("MV10") else "full",
                    pressure_control=m.pressure_control,
                    pressure_setpoint=m.pressure_setpoint if m.pressure_setpoint > 0 else NAN,
                    pressure_gauge=m.pressure_gauge,
                    pressure_mode=("high (PID)" if high else "low (table)") if m.pressure_setpoint > 0 else "",
                    flow_min=self.flow_limits[0], flow_max=self.flow_limits[1],
                ),
                sample_shutter_away=m.shutter_open,
                interlocks=[f"laser {code} {meaning}" for code, meaning in filter(None, [self.laser_interlock])],
                excimer=self._excimer(), read_only=self.read_only,
            )

    def _excimer_opmode(self) -> tuple[str, bool]:
        """(OPMODE, waiting), resolving OFF,WAIT into ON once the warm-up is over."""
        if self.laser_interlock is not None:
            self._laser_on_at = None
            self.excimer["opmode"] = "OFF"
        elif self._laser_on_at is not None:
            if time.monotonic() < self._laser_on_at:
                return "OFF,WAIT", True
            self._laser_on_at = None
            self.excimer["opmode"] = "ON"
        return self.excimer["opmode"], False

    def _excimer(self) -> pb.Excimer:
        opmode, waiting = self._excimer_opmode()
        status, codes = opmode, []
        if self.laser_interlock is not None:
            code, meaning = self.laser_interlock
            status = f"OFF ({code} {meaning})"
            codes = [pb.LaserCode(code=code, kind="interlock-hw", meaning=meaning)]
        return pb.Excimer(
            present=True, connected=True, opmode=opmode, waiting=waiting, status=status, codes=codes,
            mode=self.excimer["mode"], hv=self.excimer["hv"], energy_set=self.excimer["energy_set"],
            energy=self.excimer["energy_set"] if opmode == "ON" else 0.0,
            control=self.excimer["control"], trigger="EXT", hv_min=HV_CAPS[0], hv_max=HV_CAPS[1],
            energy_min=ENERGY_CAPS[0], energy_max=ENERGY_CAPS[1], version="lumi-sim",
        )

    def set_laser_interlock(self, code: int, meaning: str) -> None:
        """Open (code > 0) or clear (code 0) a laser interlock; it drops the laser to OFF."""
        self.laser_interlock = (code, meaning) if code else None
        if code:
            self._emit("interlock", f"laser {code} {meaning}")

    def GetState(self, request, context):
        return self._state()

    def WatchState(self, request, context):
        period = 1.0 / request.rate_hz if request.rate_hz > 0 else 0.1
        while context.is_active():
            yield self._state()
            time.sleep(period)

    def WatchEvents(self, request, context):
        q: queue.Queue = queue.Queue()
        self._subscribers.append(q)
        try:
            while context.is_active():
                try:
                    yield q.get(timeout=0.5)
                except queue.Empty:
                    continue
        finally:
            self._subscribers.remove(q)

    # --- control ------------------------------------------------------------------

    def AcquireControl(self, request, context):
        with self._lock:
            if self.controller and self.controller != request.client and not request.force:
                return pb.ControlReply(granted=False, controller=self.controller,
                                       message=f"control is held by {self.controller}")
            taken = f" (taken from {self.controller})" if self.controller and self.controller != request.client else ""
            self.controller = request.client
        self._emit("control", f"{request.client} has control{taken}")
        return pb.ControlReply(granted=True, controller=self.controller, message=f"{request.client} has control{taken}")

    def ReleaseControl(self, request, context):
        with self._lock:
            if self.controller == request.client:
                self.controller = ""
                self._emit("control", f"{request.client} released control")
        return pb.ControlReply(granted=False, controller=self.controller, message="released")

    # --- vacuum -------------------------------------------------------------------

    def SetValve(self, request, context):
        self._require_control(context)
        if request.valve not in self.valves:
            return self._no(f"unknown valve {request.valve!r}")
        self.valves[request.valve] = request.open
        self._emit("valve_changed", f"{request.valve} {'open' if request.open else 'closed'}")
        return self._ok()

    # --- motion -------------------------------------------------------------------

    def SelectTarget(self, request, context):
        return self._motion(request, context, lambda: self.model.select_target(request.target), f"target {request.target}")

    def SetMaskPosition(self, request, context):
        return self._motion(request, context, lambda: self.model.move_mask(1, request.position), f"Mask1 {request.position:g}")

    def SetSamplePosition(self, request, context):
        return self._motion(request, context, lambda: self.model.set_sample_angle(request.position),
                            f"sample {request.position:g}")

    def SetRheedGunX(self, request, context):
        return self._motion(request, context, lambda: self.model.move_rheed_gun_x(request.position),
                            f"RHEED gun X {request.position:g}")

    def HomeAxis(self, request, context):
        home = {
            "TG": lambda: self.model.select_target("Clear"),
            "Mask1": lambda: self.model.move_mask(1, 0.0),
            "RHEED gun X": lambda: self.model.move_rheed_gun_x(0.0),
        }.get(request.axis)
        if home is None:
            self._require_control(context)
            return self._no(f"unknown axis {request.axis!r}")
        return self._motion(request, context, home, f"{request.axis} home")

    def SetTargetSpin(self, request, context):
        self._require_control(context)
        self.model.set_target_rotation_mode("ON" if request.on else "OFF")
        return self._ok()

    def SetTargetTwist(self, request, context):
        self._require_control(context)
        self.model.set_target_twist_mode("ON" if request.on else "OFF")
        return self._ok()

    def SetMotorFree(self, request, context):
        self._require_control(context)
        self.model.motor_lock_released = bool(request.on)
        return self._ok()

    def SetTargetAutoMotion(self, request, context):
        self._require_control(context)
        self.auto_spin, self.auto_twist = request.spin, request.twist
        self.model.set_target_rotation_mode("AUTO" if request.spin else "OFF")
        self.model.set_target_twist_mode("AUTO" if request.twist else "OFF")
        return self._ok()

    # --- heater -------------------------------------------------------------------

    def SetHeaterKey(self, request, context):
        self._require_control(context)
        self.model.set_heating_lock(not request.on)
        return self._ok()

    def SetHeaterStart(self, request, context):
        self._require_control(context)
        if request.on and self.model.heating_locked:
            return self._no("heater key is off (SetHeaterKey on first)")
        self.model.set_heating_laser(request.on)
        return self._ok()

    def SetHeaterThreshold(self, request, context):
        self._require_control(context)
        self.threshold_on = bool(request.on)
        return self._ok()

    def SetHeatingCurrent(self, request, context):
        self._require_control(context)
        if not 0 <= request.amps <= self.model.config.current_max:
            return self._no(f"current {request.amps:g} A is outside 0..{self.model.config.current_max:g} A")
        self.model.set_temperature_control("Manual")
        self.model.set_heating_current(request.amps)
        return self._ok()

    def SetHeaterCurrentLimits(self, request, context):
        self._require_control(context)
        if request.min_amps > request.max_amps:
            return self._no("min_amps is above max_amps")
        self.model.set_current_limits(minimum=request.min_amps, maximum=request.max_amps)
        return self._ok()

    def SetTemperature(self, request, context):
        self._require_control(context)
        m = self.model
        m.set_temperature_tolerance(request.tolerance or 5.0)
        m.set_temperature_ramp(request.ramp_c_per_min if request.ramp_c_per_min > 0 else None,
                               request.ramp_c_per_min > 0)
        m.set_temperature_control("PID")
        m.set_temperature(request.celsius)
        if request.wait and not self._wait(m.at_temperature):
            return self._no(f"did not reach {request.celsius:g} C in time")
        return self._ok(f"PID towards {request.celsius:g} C")

    def SetTemperatureControl(self, request, context):
        self._require_control(context)
        m = self.model
        if request.on:
            m.set_temperature_control("PID")
        else:
            with m._lock:
                m.manual_current = m.current_command  # manual at the present current
            m.set_temperature_control("Manual")
        return self._ok()

    def SetSampleShutter(self, request, context):
        self._require_control(context)
        self.model.set_shutter(not request.in_front)
        if not self._wait(lambda: self.model.shutter_idle):
            return self._no("shutter did not finish moving")
        return self._ok()

    # --- deposition laser ---------------------------------------------------------

    def FireLaser(self, request, context):
        self._require_control(context)
        if request.pulses <= 0 or request.rate_hz <= 0:
            return self._no("pulses and rate_hz must be positive")
        expected = self.model.trigger_laser(request.pulses, request.rate_hz)
        if request.wait:
            if not self._wait(lambda: self.model.laser_idle, (expected * 2 + 60.0) / self.time_scale):
                return self._no("pulse train did not finish in time")
            self._emit("laser_done", f"{request.pulses} pulses at {request.rate_hz:g} Hz")
        return self._ok()

    # --- gas ----------------------------------------------------------------------

    def SetMfcFlow(self, request, context):
        self._require_control(context)
        if request.mfc != 1:
            return self._no("only MFC1 is used in this chamber")
        self.model.set_mfc_control(True)
        self.model.set_mfc_flow(1, request.sccm)
        return self._ok()

    def _apply_regime(self, torr: float) -> None:
        high = torr >= HIGH_REGIME_MIN
        self.ig1_on = not high
        self.valves["MV10"] = not high
        self.valves["MV11"] = True

    def SetPressure(self, request, context):
        self._require_control(context)
        torr = request.torr
        if LOW_REGIME_MAX < torr < HIGH_REGIME_MIN:
            return self._no(f"{torr:.2e} Torr is between the low (<= {LOW_REGIME_MAX:.0e}) and "
                            f"high (>= {HIGH_REGIME_MIN:.0e}) regimes")
        if torr > 1.0:
            return self._no("above 1 Torr")
        m = self.model
        if request.gauge:
            m.select_pressure_gauge(request.gauge)
        m.set_pressure(torr)
        if m.pressure_control:
            self._apply_regime(torr)
        if request.wait:
            tolerance = request.tolerance or 0.1
            if not self._wait(lambda: abs(m.true_pressure - torr) <= tolerance * torr):
                return self._no(f"pressure did not settle at {torr:.2e} Torr in time")
        return self._ok()

    def SetPressureControl(self, request, context):
        self._require_control(context)
        m = self.model
        if request.on:
            self._apply_regime(m.pressure_setpoint)
            m.set_mfc_control(True)
        m.set_pressure_control(request.on)
        return self._ok()

    def SetPressureFlowLimits(self, request, context):
        self._require_control(context)
        self.flow_limits = (request.min_sccm, request.max_sccm)
        return self._ok()

    def TunePressure(self, request, context):
        self._require_control(context)
        return self._ok("auto-tune is not simulated; the simulator's pressure control is ideal")

    def SetIonGauge(self, request, context):
        self._require_control(context)
        if request.gauge == "IG1":
            self.ig1_on = bool(request.on)
        elif request.gauge == "IG3":
            self.ig3_on = bool(request.on)
        else:
            return self._no(f"unknown ion gauge {request.gauge!r}")
        return self._ok()

    # --- excimer ------------------------------------------------------------------

    def _laser(self, context, change) -> pb.OperationReply:
        self._require_control(context)
        if not self.excimer["control"]:
            return self._no("the laser's other port has control (CM=1): take laser control first")
        problem = change()
        return self._no(problem) if problem else self._ok()

    def TakeLaserControl(self, request, context):
        self._require_control(context)
        self.excimer["control"] = True
        return self._ok("CM=2")

    def ReleaseLaser(self, request, context):
        self._require_control(context)
        self.excimer["control"] = False
        return self._ok("CM=1")

    def _laser_preflight(self) -> str | None:
        if self.laser_interlock is not None:
            code, meaning = self.laser_interlock
            return f"laser interlock {code} {meaning}"
        mode = self.excimer["mode"]
        if mode == "HV PGR":
            if not HV_CAPS[0] <= self.excimer["hv"] <= HV_CAPS[1]:
                return f"HV set point {self.excimer['hv']:g} kV is outside {HV_CAPS[0]}..{HV_CAPS[1]} kV"
        elif mode == "EGY PGR":
            if not ENERGY_CAPS[0] <= self.excimer["energy_set"] <= ENERGY_CAPS[1]:
                return (f"energy set point {self.excimer['energy_set']:g} mJ is outside "
                        f"{ENERGY_CAPS[0]:g}..{ENERGY_CAPS[1]:g} mJ")
        else:
            return f"running mode {mode!r} is not HV PGR or EGY PGR"
        return None

    def LaserOn(self, request, context):
        def change():
            problem = self._laser_preflight()
            if problem:
                return problem
            if self._excimer_opmode()[0] != "ON":
                self._laser_on_at = time.monotonic() + LASER_WARMUP_S / self.time_scale
            return None
        reply = self._laser(context, change)
        if not reply.ok or not request.wait:
            return reply
        if not self._wait(lambda: self._excimer_opmode()[0] == "ON" or self.laser_interlock is not None,
                          LASER_ON_TIMEOUT_S / self.time_scale):
            return self._no(f"laser not ON after {LASER_ON_TIMEOUT_S:g} s")
        if self.laser_interlock is not None:
            code, meaning = self.laser_interlock
            return self._no(f"laser interlock {code} {meaning} while switching on")
        return self._ok("ON")

    def LaserStandby(self, request, context):
        return self._laser(context, lambda: self._laser_to("STANDBY"))

    def LaserOff(self, request, context):
        return self._laser(context, lambda: self._laser_to("OFF"))

    def _laser_to(self, opmode: str) -> None:
        self._laser_on_at = None
        self.excimer["opmode"] = opmode

    def SetLaserMode(self, request, context):
        modes = {"hv": "HV PGR", "energy": "EGY PGR"}
        if request.mode not in modes:
            self._require_control(context)
            return self._no(f"mode must be hv or energy, not {request.mode!r}")
        return self._laser(context, lambda: self.excimer.update(mode=modes[request.mode]))

    def SetLaserHv(self, request, context):
        def change():
            if self.excimer["mode"] != "HV PGR":
                return "HV set point is settable in hv mode only"
            if not HV_CAPS[0] <= request.kv <= HV_CAPS[1]:
                return f"{request.kv:g} kV is outside {HV_CAPS[0]}..{HV_CAPS[1]} kV"
            self.excimer["hv"] = request.kv
            return None
        return self._laser(context, change)

    def SetLaserEnergy(self, request, context):
        def change():
            if not ENERGY_CAPS[0] <= request.mj <= ENERGY_CAPS[1]:
                return f"{request.mj:g} mJ is outside {ENERGY_CAPS[0]:g}..{ENERGY_CAPS[1]:g} mJ"
            self.excimer["energy_set"] = request.mj
            return None
        return self._laser(context, change)


class SimPldService:
    """The servicer on a gRPC server bound to an ephemeral loopback port, plus the
    model's ticker. `address` is what PldClient connects to."""

    def __init__(self, model: ChamberModel, *, time_scale: float = 1.0, tick_interval: float = 0.05,
                 command_timeout: float = 3600.0, address: str = "127.0.0.1:0") -> None:
        self.model = model
        self.servicer = SimChamberServicer(model, time_scale=time_scale, command_timeout=command_timeout)
        self.ticker = ModelTicker(model, time_scale=time_scale, tick_interval=tick_interval)
        # Enough workers for the long-lived streams (state, events) plus blocking waits.
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="sim-pld"))
        pb_grpc.add_ChamberServicer_to_server(self.servicer, self.server)
        host = address.rsplit(":", 1)[0]
        port = self.server.add_insecure_port(address)
        self.address = f"{host}:{port}"

    def start(self) -> None:
        self.ticker.start()
        self.server.start()
        log.info("[sim] pld-service stand-in on %s", self.address)

    def stop(self) -> None:
        self.server.stop(grace=1.0)
        self.ticker.stop()
