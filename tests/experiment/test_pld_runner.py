"""PldCommandRunner: the MI commands `ExperimentManager` issues, as chamber.pld ops.

T0: no broker. The generated `ChamberPldClient` is stood in for by a fake that records
every op and answers ok, so these tests are about the translation alone -- which op,
with which fields, in which order.
"""

from __future__ import annotations

import pytest

from lumi.base.mq.client import RemoteError
from lumi.contracts.payloads.pld import PldChamberState, PldHeater, PldMotion, PldReply
from lumi.experiment.pld import PldCommandRunner, PldRefused
from lumi.pascal import command as pcmd

pytestmark = pytest.mark.asyncio


class FakePldClient:
    def __init__(self, state: PldChamberState | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.state = state or PldChamberState(connected=True, backend="TWIN")
        self.refuse: dict[str, str] = {}
        self.raise_remote: str | None = None

    async def call(self, op, req=None):
        if self.raise_remote:
            raise RemoteError("chamber.pld", op, "RuntimeError", self.raise_remote)
        self.calls.append((op, req.model_dump() if req is not None else {}))
        if op in self.refuse:
            return PldReply(ok=False, message=self.refuse[op])
        return PldReply(ok=True, message="done")

    async def chamber_state(self, req=None):
        return self.state


def ops(client):
    return [op for op, _ in client.calls]


async def test_to_temperature_ramps_from_where_the_substrate_is():
    # The exact sequence ExperimentManager.to_temperature issues, PID off at the start.
    client = FakePldClient()
    runner = PldCommandRunner(client)
    await runner.execute(pcmd.TemperatureSet(650, nowait=True))
    await runner.execute(pcmd.TemperatureRamp(20, state=pcmd.PascalState("ON")))
    await runner.execute(pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.PID))
    await runner.execute(pcmd.TemperatureSet(650, nowait=False))

    # No setpoint goes out with PID off (it would engage PID with no ramp: a jump).
    assert ops(client) == ["set_temperature_control", "set_temperature", "set_temperature"]
    assert client.calls[0][1] == {"on": True}
    assert client.calls[1][1] == {"celsius": 650.0, "ramp_c_per_min": 20.0, "wait": False, "tolerance": 0.0}
    assert client.calls[2][1]["wait"] is True and client.calls[2][1]["ramp_c_per_min"] == 20.0


async def test_engaging_pid_alone_holds_the_present_temperature():
    # initiate_heating_laser: PID on with no new target must not reuse an old one.
    client = FakePldClient()
    runner = PldCommandRunner(client)
    await runner.execute(pcmd.TemperatureSet(800, nowait=True))  # stored (PID off)
    await runner.execute(pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.MANUAL))
    await runner.execute(pcmd.TemperatureControl(mode=pcmd.TemperatureControlModeType.PID))
    assert "set_temperature" not in ops(client)


async def test_a_setpoint_with_pid_on_goes_straight_out():
    client = FakePldClient(PldChamberState(heater=PldHeater(pid_on=True)))
    runner = PldCommandRunner(client)
    await runner.execute(pcmd.TemperatureRamp(10, state=pcmd.PascalState("ON")))
    await runner.execute(pcmd.TemperatureSet(160, nowait=False))  # cool_down
    assert client.calls == [("set_temperature", {
        "celsius": 160.0, "ramp_c_per_min": 10.0, "wait": True, "tolerance": 0.0})]


async def test_manual_current_switches_pid_off():
    client = FakePldClient(PldChamberState(heater=PldHeater(pid_on=True)))
    runner = PldCommandRunner(client)
    await runner.execute(pcmd.SetHeatingCurrent(current=7.5))
    await runner.execute(pcmd.TemperatureSet(300, nowait=True))
    assert ops(client) == ["set_heating_current"]


async def test_heater_switches():
    client = FakePldClient()
    runner = PldCommandRunner(client)
    await runner.execute(pcmd.HeatingLaserLock(locked=False, nowait=False))
    await runner.execute(pcmd.HeatingLaser(state=pcmd.PascalState("ON"), nowait=False))
    await runner.execute(pcmd.HeatingLaserThreshold(state=pcmd.PascalState("OFF")))
    assert client.calls == [
        ("set_heater_key", {"on": True}),
        ("set_heater_start", {"on": True}),
        ("set_heater_threshold", {"on": False}),
    ]


async def test_shutter_on_means_open():
    client = FakePldClient()
    runner = PldCommandRunner(client)
    await runner.execute(pcmd.SampleShutter(pcmd.PascalState("ON")))
    await runner.execute(pcmd.SampleShutter(pcmd.PascalState("OFF")))
    assert client.calls == [("set_sample_shutter", {"in_front": False}),
                            ("set_sample_shutter", {"in_front": True})]


async def test_motion_and_nowait():
    client = FakePldClient(PldChamberState(motion=PldMotion(sample_angle=350.0)))
    runner = PldCommandRunner(client)
    await runner.execute(pcmd.SelectTarget("C", nowait=False))
    await runner.execute(pcmd.SetMaskPosition(mask_id=pcmd.MaskID.M1, distance=12.0, sync=False, nowait=True))
    await runner.execute(pcmd.RotateSample(20, sync=False, nowait=False))
    await runner.execute(pcmd.SetRHEEDGunX(3.5))
    assert client.calls == [
        ("select_target", {"target": "C", "wait": True}),
        ("set_mask_position", {"position": 12.0, "wait": False}),
        ("set_sample_position", {"position": pytest.approx(10.0), "wait": True}),
        ("set_rheed_gun_x", {"position": 3.5, "wait": True}),
    ]
    with pytest.raises(PldRefused, match="Mask1 only"):
        await runner.execute(pcmd.SetMaskPosition(mask_id=pcmd.MaskID.M2, distance=1.0, sync=False, nowait=False))


async def test_target_auto_motion():
    client = FakePldClient()
    runner = PldCommandRunner(client)
    await runner.execute(pcmd.TargetRotationMode(mode=pcmd.TargetRotationModeType.AUTO))
    await runner.execute(pcmd.TargetTwistMode(mode=pcmd.TargetTwistModeType.AUTO))
    assert client.calls[-1] == ("set_target_auto_motion", {"spin": True, "twist": True})
    await runner.execute(pcmd.TargetTwistMode(mode=pcmd.TargetTwistModeType.OFF))
    assert client.calls[-2:] == [("set_target_auto_motion", {"spin": True, "twist": False}),
                                 ("set_target_twist", {"on": False})]


async def test_laser_gas_and_logging():
    client = FakePldClient()
    runner = PldCommandRunner(client)
    await runner.execute(pcmd.TriggerLaser(num_pulse=300, frequency=5, sync=False, nowait=False))
    await runner.execute(pcmd.SetMFC1Flow(2.5))
    await runner.execute(pcmd.SetMFCControl(enable=False))
    await runner.execute(pcmd.SetPressure(1e-2))
    await runner.execute(pcmd.PressureControl(state=pcmd.PascalState(True)))
    await runner.execute(pcmd.SetLogInterval(2))
    await runner.execute(pcmd.DataLogging("run.csv", pcmd.PascalState("ON")))
    await runner.execute(pcmd.DataLogging("", pcmd.PascalState("OFF")))
    assert client.calls == [
        ("fire_laser", {"pulses": 300, "rate_hz": 5.0, "wait": True}),
        ("set_mfc_flow", {"mfc": 1, "sccm": 2.5}),
        ("set_mfc_flow", {"mfc": 1, "sccm": 0.0}),
        ("set_pressure", {"torr": 0.01, "gauge": "", "wait": False, "tolerance": 0.0}),
        ("set_pressure_control", {"on": True}),
        ("start_logging", {"file_name": "run.csv", "interval_s": 2.0}),
        ("stop_logging", {}),
    ]
    with pytest.raises(PldRefused, match="MFC2"):
        await runner.execute(pcmd.SetMFC2Flow(1.0))


async def test_a_refusal_raises_with_the_services_reason():
    client = FakePldClient()
    client.refuse["select_target"] = "motor holding lock is released"
    with pytest.raises(PldRefused, match="holding lock"):
        await PldCommandRunner(client).execute(pcmd.SelectTarget("A", nowait=False))


async def test_a_command_with_no_equivalent_names_the_fallback():
    with pytest.raises(NotImplementedError, match="--use-legacy-mi-mode"):
        await PldCommandRunner(FakePldClient()).execute(pcmd.Beep())


async def test_a_legacy_chamber_node_says_which_flag_to_pass():
    client = FakePldClient()
    client.raise_remote = "this chamber node runs --use-legacy-mi-mode, so the pld-service driver is off"
    with pytest.raises(RuntimeError, match="experiment node defaults to the pld driver"):
        await PldCommandRunner(client).execute(pcmd.SelectTarget("A", nowait=False))


@pytest.mark.parametrize("state,status", [
    (PldChamberState(connected=True, backend="TWIN"), "on"),
    (PldChamberState(connected=False), "off"),
    (PldChamberState(connected=True, read_only=True), "off"),
    (PldChamberState(connected=True, controller="gui"), "busy"),
    (PldChamberState(connected=True, controller="lumi-pascal"), "on"),
])
async def test_check(state, status):
    got, message = await PldCommandRunner(FakePldClient(state)).check()
    assert got == status, message
