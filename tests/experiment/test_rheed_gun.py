"""rheed_on / rheed_off: the order, the emission search, and what a failure leaves.

T0: no broker. A fake pld-service gun whose ramps arrive at once and whose emission
current rises with the filament up to a saturation point.
"""

from __future__ import annotations

import pytest

from lumi.contracts.payloads.pld import PldChamberState, PldRheed
from lumi.experiment.chamber import PldRefused
from lumi.experiment.rheed_gun import RheedGun, RheedGunSettings, emission_table, leading_digits

FAST = RheedGunSettings(emission_settle_s=0.0, poll_s=0.0, ramp_timeout_s=0.05)


class FakeGun:
    """pld-service's RHEED ops on a PldRheed; the emission saturates at `knee` mA."""

    driver = "pld"

    def __init__(self, knee: float = 1950.0, emission: bool = True, **rheed) -> None:
        self.r = PldRheed(present=True, link_ok=True, filament_min_ma=507, filament_max_ma=2500,
                          hv_max_kv=30, **rheed)
        self.knee, self.has_emission, self.calls = knee, emission, []
        self.refuse: dict[str, str] = {}
        self.stuck_hv: float | None = None

    async def state(self) -> PldChamberState:
        r = self.r
        if r.filament_on and r.hv_on and self.has_emission:
            # 30, 35, 40 ... uA: the second digit moves with each 50 mA step, until the knee.
            r.emission_ua = round((min(r.filament_set_ma, self.knee) - 1500) / 10.0 + 0.123, 3)
        else:
            r.emission_ua = None if not self.has_emission else 0.0
        return PldChamberState(rheed=r)

    async def _call(self, op: str, req=None):
        self.calls.append((op, req.model_dump() if req is not None else {}))
        if op in self.refuse:
            raise PldRefused(f"pld-service refused {op}: {self.refuse[op]}")
        r = self.r
        if op in ("rheed_filament_on", "set_rheed_filament"):
            r.filament_on = True
            r.filament_set_ma = r.filament_target_ma = r.filament_readback_ma = req.ma
        elif op in ("rheed_hv_on", "set_rheed_hv"):
            assert r.filament_on, "HV needs the filament"
            r.hv_on = True
            r.hv_set_kv = r.hv_target_kv = req.kv
            r.hv_readback_kv = self.stuck_hv if self.stuck_hv is not None else req.kv
        elif op == "set_rheed_focus":
            r.focus_on, r.focus_set_ma, r.focus_readback_ma = True, req.ma, req.ma
        elif op == "set_rheed_deflection":
            r.x_on, r.x_set_ma, r.x_readback_ma = True, req.x_ma, req.x_ma
            r.y_on, r.y_set_ma, r.y_readback_ma = True, req.y_ma, req.y_ma
        elif op == "rheed_all_off_gentle":
            r.hv_on = r.filament_on = False
            r.hv_readback_kv, r.filament_readback_ma = 0.0, 0.0
        else:
            raise AssertionError(op)

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]


async def test_on_goes_filament_hv_search_then_optics_and_stops_at_saturation():
    gun = FakeGun(knee=1950)
    result = await RheedGun(gun, FAST).on()
    assert gun.ops()[:2] == ["rheed_filament_on", "rheed_hv_on"]
    assert gun.calls[0][1] == {"ma": 1800.0} and gun.calls[1][1] == {"kv": 25.0}
    tried = [s["filament_ma"] for s in result["emission_steps"]]
    # 1850, 1900, 1950 still raise the emission; 2000 adds nothing and is taken back.
    assert tried == [1800, 1850, 1900, 1950, 2000]
    assert result["filament_ma"] == 1950 and gun.r.filament_set_ma == 1950
    assert "saturated" in result["saturation"]
    assert result["message"].endswith("1950 | 45.12\n       2000 | 45.12")
    assert gun.ops()[-2:] == ["set_rheed_focus", "set_rheed_deflection"]
    assert (gun.r.focus_set_ma, gun.r.x_set_ma, gun.r.y_set_ma) == (476.0, -61.0, 223.0)
    assert result["gun"]["hv_kv"] == 25.0 and result["gun"]["filament_on"]


async def test_the_search_stops_at_the_maximum_while_still_rising():
    gun = FakeGun(knee=5000)
    result = await RheedGun(gun, FAST).on()
    assert result["filament_ma"] == 2100 and "maximum" in result["saturation"]


async def test_no_emission_reading_leaves_the_filament_at_the_start():
    gun = FakeGun(emission=False)
    result = await RheedGun(gun, FAST).on()
    assert result["filament_ma"] == 1800 and "no emission reading" in result["saturation"]
    assert "set_rheed_filament" not in gun.ops()


async def test_overrides_are_for_this_call():
    gun = FakeGun()
    result = await RheedGun(gun, FAST).on(hv_kv=20, focus_ma=450, deflection_x_ma=-50,
                                          deflection_y_ma=None)
    assert gun.r.hv_set_kv == 20 and gun.r.focus_set_ma == 450 and gun.r.x_set_ma == -50
    assert gun.r.y_set_ma == 223.0 and result["settings"]["hv_kv"] == 20


async def test_a_gun_already_on_is_set_rather_than_switched_on():
    gun = FakeGun(filament_on=True, filament_set_ma=1000.0, hv_on=True, hv_set_kv=15.0)
    await RheedGun(gun, FAST).on()
    assert gun.ops()[:2] == ["set_rheed_filament", "set_rheed_hv"]


@pytest.mark.parametrize("state, why", [
    ({"present": False}, "no RHEED link"),
    ({"read_only": True}, "read-only"),
    ({"link_ok": False}, "did not answer"),
    ({"interlock": True}, "interlock"),
    ({"filament_open": True}, "open"),
    ({"all_ramping_down": True}, "ramping down"),
])
async def test_on_is_refused_with_the_reason(state, why):
    gun = FakeGun()
    for k, v in state.items():
        setattr(gun.r, k, v)
    with pytest.raises(RuntimeError, match=why):
        await RheedGun(gun, FAST).on()
    assert gun.calls == []


async def test_values_past_the_guns_limits_are_refused_before_anything_moves():
    gun = FakeGun()
    with pytest.raises(ValueError, match="above the gun's limit"):
        await RheedGun(gun, FAST).on(filament_max_ma=3000)
    with pytest.raises(ValueError, match="above filament_max_ma"):
        await RheedGun(gun, FAST).on(filament_start_ma=2200)
    assert gun.calls == []


async def test_a_ramp_that_does_not_arrive_fails_saying_where_and_leaves_the_gun_on():
    gun = FakeGun()
    gun.stuck_hv = 12.0
    with pytest.raises(RuntimeError, match=r"stopped at the HV step.*not there.*left as it is.*HV on 12 kV"):
        await RheedGun(gun, FAST).on()
    assert gun.r.filament_on and gun.r.hv_on
    assert "rheed_all_off_gentle" not in gun.ops()


async def test_a_refusal_midway_is_reported_with_the_stage():
    gun = FakeGun()
    gun.refuse["set_rheed_focus"] = "focus link down"
    with pytest.raises(RuntimeError, match="stopped at the focus step: pld-service refused set_rheed_focus"):
        await RheedGun(gun, FAST).on()


async def test_off_is_the_gentle_all_off_and_waits_for_filament_and_hv():
    gun = FakeGun(filament_on=True, filament_set_ma=1950.0, hv_on=True, hv_set_kv=25.0)
    result = await RheedGun(gun, FAST).off()
    assert gun.ops() == ["rheed_all_off_gentle"]
    assert not result["gun"]["filament_on"] and not result["gun"]["hv_on"]


@pytest.mark.parametrize("before, after, saturated", [
    (8.41, 8.47, True),     # 8.4 and 8.4
    (8.41, 8.52, False),    # 8.4 -> 8.5
    (8.4036, 8.6652, False),  # the twin at 2050 -> 2100 mA
    (9.95, 10.02, False),   # into the next decade
    (12.3, 12.9, True),
    (0.0324, 0.0329, True),
])
def test_saturation_is_the_first_two_digits_not_changing(before, after, saturated):
    assert (leading_digits(after, 2) <= leading_digits(before, 2)) is saturated


def test_the_emission_table_lists_current_against_emission():
    table = emission_table([{"filament_ma": 1800.0, "emission_ua": 6.3108},
                            {"filament_ma": 1850.0, "emission_ua": None}])
    assert table.splitlines() == ["filament mA | emission uA", "       1800 | 6.311", "       1850 | -"]


async def test_by_person_stops_after_filament_and_hv_and_steps_only_when_asked():
    gun = FakeGun()
    g = RheedGun(gun, FAST)
    result = await g.on(by_person=True)
    assert result["searching"] and g.searching
    assert gun.ops() == ["rheed_filament_on", "rheed_hv_on"]
    assert result["message"].endswith("filament mA | emission uA\n       1800 | 30.12")
    result = await g.step_up()
    assert [s["filament_ma"] for s in result["emission_steps"]] == [1800, 1850]
    assert "still rising (30.12 -> 35.12 uA)" in result["message"]
    for _ in range(3):
        result = await g.step_up()  # 1900, 1950, then 2000: past the knee
    assert "did not change (45.12 -> 45.12 uA)" in result["saturation"]
    assert "stop at 1950 mA?" in result["saturation"]
    assert "set_rheed_focus" not in gun.ops()  # nothing past the search until the person says stop

    result = await g.finish(1950)
    assert not g.searching and not result["searching"]
    assert gun.r.filament_set_ma == 1950 and "the person stopped the search at 1950 mA" in result["message"]
    assert gun.ops()[-2:] == ["set_rheed_focus", "set_rheed_deflection"]


async def test_the_person_can_only_finish_at_a_tried_step():
    g = RheedGun(FakeGun(), FAST)
    await g.on(by_person=True)
    with pytest.raises(ValueError, match="not one of the tried steps"):
        await g.finish(1825)
    assert g.searching


async def test_the_search_stops_at_the_maximum_for_a_person_too():
    g = RheedGun(FakeGun(knee=5000), FAST.with_overrides(filament_max_ma=1850))
    await g.on(by_person=True)
    await g.step_up()
    result = await g.step_up()
    assert "at its maximum, 1850 mA" in result["message"] and len(result["emission_steps"]) == 2


async def test_no_search_no_step_and_off_ends_a_search():
    gun = FakeGun()
    g = RheedGun(gun, FAST)
    with pytest.raises(RuntimeError, match="no emission search is open"):
        await g.step_up()
    await g.on(by_person=True)
    with pytest.raises(RuntimeError, match="already open"):
        await g.on(by_person=True)
    await g.off()
    assert not g.searching


def test_settings_from_the_toml_section():
    s = RheedGunSettings.from_settings({"hv_kv": 20, "filament_by_person": False, "emission_digits": 2})
    assert s.hv_kv == 20.0 and s.filament_by_person is False and s.focus_ma == 476.0
    with pytest.raises(RuntimeError, match="unknown keys: hv"):
        RheedGunSettings.from_settings({"hv": 20})
