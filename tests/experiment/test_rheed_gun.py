"""rheed_on / rheed_off: the order, the emission search, and what a failure leaves.

T0: no broker. A fake pld-service gun whose ramps arrive at once and whose emission
current rises with the filament up to a saturation point.
"""

from __future__ import annotations

import pytest

from lumi.contracts.payloads.pld import PldChamberState, PldRheed
from lumi.experiment.chamber import PldRefused
from lumi.experiment.rheed_gun import (
    RheedGun,
    RheedGunSettings,
    emission_table,
    frame_intensity,
    leading_digits,
    plateau_estimate,
    saturation_hint,
)

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
    result = await g.on(judge="person")
    assert result["searching"] and g.searching
    assert gun.ops() == ["rheed_filament_on", "rheed_hv_on"]
    assert result["message"].endswith("filament mA | emission uA\n       1800 | 30.12")
    result = await g.step_up()
    assert [s["filament_ma"] for s in result["emission_steps"]] == [1800, 1850]
    assert "still rising (emission rising (30.12 -> 35.12 uA, +16.6%; no plateau estimate yet" in result["message"]
    for _ in range(3):
        result = await g.step_up()  # 1900, 1950, then 2000: past the knee
    assert "emission flat (45.12 -> 45.12 uA, +0.0%" in result["saturation"]
    assert "stop at 1950 mA?" in result["saturation"]
    assert "set_rheed_focus" not in gun.ops()  # nothing past the search until the person says stop

    result = await g.finish(1950)
    assert not g.searching and not result["searching"]
    assert gun.r.filament_set_ma == 1950 and "the person stopped the search at 1950 mA" in result["message"]
    assert gun.ops()[-2:] == ["set_rheed_focus", "set_rheed_deflection"]


async def test_the_person_can_only_finish_at_a_tried_step():
    g = RheedGun(FakeGun(), FAST)
    await g.on(judge="person")
    with pytest.raises(ValueError, match="not one of the tried steps"):
        await g.finish(1825)
    assert g.searching


async def test_the_search_stops_at_the_maximum_for_a_person_too():
    g = RheedGun(FakeGun(knee=5000), FAST.with_overrides(filament_max_ma=1850))
    await g.on(judge="person")
    await g.step_up()
    result = await g.step_up()
    assert "at its maximum, 1850 mA" in result["message"] and len(result["emission_steps"]) == 2


async def test_no_search_no_step_and_off_ends_a_search():
    gun = FakeGun()
    g = RheedGun(gun, FAST)
    with pytest.raises(RuntimeError, match="no emission search is open"):
        await g.step_up()
    await g.on(judge="person")
    with pytest.raises(RuntimeError, match="already open"):
        await g.on(judge="person")
    await g.off()
    assert not g.searching


def test_settings_from_the_toml_section():
    s = RheedGunSettings.from_settings({"hv_kv": 20, "filament_judge": "person", "emission_digits": 2})
    assert s.hv_kv == 20.0 and s.filament_judge == "person" and s.focus_ma == 476.0
    assert RheedGunSettings().filament_judge == "agent"
    with pytest.raises(ValueError, match="not one of agent, person, rule"):
        RheedGunSettings.from_settings({"filament_judge": "nobody"})
    with pytest.raises(RuntimeError, match="unknown keys: hv"):
        RheedGunSettings.from_settings({"hv": 20})


class FakeCamera:
    """The RHEED image's mean follows the beam current, up to `knee` (None: no saturation)."""

    def __init__(self, gun: FakeGun, knee: float | None = None, fail: bool = False) -> None:
        self.gun, self.knee, self.fail, self.calls = gun, knee, fail, 0

    async def __call__(self, frames: int) -> dict:
        self.calls += 1
        if self.fail:
            raise RuntimeError("no new camera frame")
        e = self.gun.r.emission_ua or 0.0
        f = self.gun.r.filament_set_ma
        if self.knee is not None and f is not None:
            e = (min(f, self.knee) - 1500) / 10.0 + 0.123
        return {"rheed_signal": round(e * 20, 3), "rheed_noise": 0.001, "rheed_headroom": 0.25,
                "rheed_clipped_px": 0}


async def test_the_agent_judges_without_a_confirmation_from_emission_and_intensity():
    gun = FakeGun(knee=1950)
    camera = FakeCamera(gun)
    g = RheedGun(gun, FAST, intensity=camera)
    result = await g.on(judge="agent")
    assert result["searching"] and result["judge"] == "agent" and g.judge == "agent"
    assert result["emission_steps"][0]["rheed_signal"] == 602.46
    assert result["message"].endswith(
        "filament mA | emission uA | RHEED signal | noise | peak/full\n"
        "       1800 | 30.12       |        602.5 |  0.1% |       25%")
    result = await g.step_up()
    assert "still rising" in result["saturation"] and "RHEED signal rising" in result["saturation"]
    assert "image peak at 25% of full scale" in result["saturation"]
    for _ in range(3):
        result = await g.step_up()  # to 2000, past the knee: both flat
    assert result["saturation"].startswith("saturated: stop at 1950 mA?")
    result = await g.finish(1950)
    assert "the agent stopped the search at 1950 mA" in result["message"] and not g.searching
    assert gun.r.filament_set_ma == 1950 and camera.calls == 5


async def test_a_camera_that_fails_leaves_the_emission_alone():
    gun = FakeGun()
    g = RheedGun(gun, FAST, intensity=FakeCamera(gun, fail=True))
    result = await g.on(judge="agent")
    assert result["emission_steps"] == [{"filament_ma": 1800.0, "emission_ua": 30.123}]


@pytest.mark.parametrize("emission, signal, noise, clipped, verdict", [
    ((8.41, 8.47), (500, 505), 0.0, 0, "saturated: stop at 1800 mA?"),
    ((8.41, 8.52), (500, 540), 0.0, 0, "still rising"),
    ((8.41, 8.47), (500, 560), 0.0, 0, "the two disagree: one more step, or stop at 1800 mA?"),
    ((8.41, 8.62), (500, 505), 0.0, 0, "the two disagree"),
    ((8.41, 8.47), (None, None), 0.0, 0, "saturated: stop at 1800 mA?"),   # no camera: the digits
    ((None, None), (500, 505), 0.0, 0, "saturated: stop at 1800 mA?"),     # no emission: the image
    ((None, None), (None, None), 0.0, 0, "nothing to compare"),
    # Clipped: the image is not judged, the emission decides.
    ((8.41, 8.52), (4000, 4090), 0.0, 35, "still rising (emission rising"),
    ((8.41, 8.52), (4000, 4090), 0.0, 35, "RHEED image not judged: 35 pixels at full scale"),
    # A dim, fast image: +8% inside twice its 5% scatter is flat, not a rise.
    ((8.41, 8.47), (50, 54), 0.05, 0, "saturated: stop at 1800 mA?"),
    ((8.41, 8.47), (50, 54), 0.05, 0, "a step under 10.0% (2x the frame-to-frame noise) is flat"),
])
def test_the_hint_sets_emission_and_intensity_side_by_side(emission, signal, noise, clipped, verdict):
    steps = [{"filament_ma": 1800.0, "emission_ua": emission[0], "rheed_signal": signal[0],
              "rheed_noise": noise},
             {"filament_ma": 1850.0, "emission_ua": emission[1], "rheed_signal": signal[1],
              "rheed_noise": noise, "rheed_clipped_px": clipped}]
    hint = saturation_hint(steps, RheedGunSettings())
    assert verdict in hint
    assert ("not judged" in hint) == bool(clipped)


@pytest.mark.parametrize("emission, signal, verdict", [
    # DRY-STO-10 at 2100 mA: no "one more step" at the cap.
    ((8.404, 8.665), (2878, 2952), "at the filament cap (2100 mA), no step left: the two disagree; "
                                   "stop at 2100 mA, or back at 2050 mA?"),
    ((8.404, 8.665), (2878, 3200), "at the filament cap (2100 mA), no step left: still rising; stop at 2100"),
    ((None, None), (None, None), "at the filament cap (2100 mA), no step left: nothing to compare"),
    ((8.41, 8.47), (2878, 2900), "saturated: stop at 2050 mA?"),
])
def test_the_hint_offers_no_step_past_the_filament_cap(emission, signal, verdict):
    steps = [{"filament_ma": 2050.0, "emission_ua": emission[0], "rheed_signal": signal[0]},
             {"filament_ma": 2100.0, "emission_ua": emission[1], "rheed_signal": signal[1]}]
    hint = saturation_hint(steps, RheedGunSettings(filament_max_ma=2100.0))
    assert hint.startswith(verdict) and "one more step" not in hint


def saturating(k, plateau=10.0, r=0.5):
    """A reading levelling off exponentially: each step adds r times the one before."""
    return plateau * (1 - 0.6 * r ** k)


def test_plateau_estimate_finds_where_an_exponential_levels_off():
    values = [saturating(k) for k in range(5)]
    plateau, reached = plateau_estimate(values)
    assert plateau == pytest.approx(10.0, rel=1e-3) and reached == pytest.approx(values[-1] / 10.0, rel=1e-3)
    assert plateau_estimate(values[:2]) is None                       # too few steps
    assert plateau_estimate([1.0, 2.0, 3.0, 4.0, 5.0]) is None        # rising steadily
    assert plateau_estimate([100.0, 100.5, 100.8], noise=0.05) is None  # within the scatter


def test_the_hint_stops_at_the_first_step_within_the_plateau_fraction():
    s = RheedGunSettings(filament_max_ma=2300.0)
    steps = [{"filament_ma": 1800.0 + 50 * k, "emission_ua": saturating(k),
              "rheed_signal": 300 * saturating(k)} for k in range(6)]
    hint = saturation_hint(steps, s)
    # 10 * (1 - 0.6 * 0.5**k): 4, 7, 8.5, 9.25, 9.63, 9.81 -- 90% of 10 first at k = 3, 1950 mA.
    assert hint.startswith("saturated: stop at 1950 mA?")
    assert "emission at 98% of its plateau (~10 uA); within 10% of it" in hint
    hint = saturation_hint(steps[:3], s)  # k = 2: 85%
    assert hint.startswith("still rising") and "not yet within 10%" in hint
    # The fraction is a setting: within 2% needs k = 5 (9.81).
    assert saturation_hint(steps, RheedGunSettings(filament_max_ma=2300.0, plateau_fraction=0.02)
                           ).startswith("saturated: stop at 2050 mA?")


def test_the_hint_on_dry_sto_10s_twin_gun_says_not_yet_at_the_cap():
    emission = [6.311, 6.899, 7.357, 7.75, 8.142, 8.404, 8.665]
    signal = [2161, 2370, 2516, 2638, 2776, 2878, 2952]
    clipped = [0, 0, 0, 0, 0, 19, 461]
    steps = [{"filament_ma": 1800.0 + 50 * k, "emission_ua": e, "rheed_signal": i, "rheed_noise": 0.004,
              "rheed_clipped_px": c} for k, (e, i, c) in enumerate(zip(emission, signal, clipped))]
    hint = saturation_hint(steps, RheedGunSettings())
    assert hint.startswith("at the filament cap (2100 mA), no step left: still rising")
    assert "not yet within 10%" in hint and "RHEED image not judged: 461 pixels" in hint


def test_frame_intensity_from_the_pixel_spread_alone():
    import numpy as np

    rng = np.random.default_rng(0)
    # Any pattern: 1% of the pixels bright, wherever they are, on a background of 100.
    frames = []
    for gain in (1.0, 1.02, 0.98):
        frame = np.full((100, 100), 100.0)
        frame.flat[rng.choice(frame.size, 100, replace=False)] = 100 + 900 * gain
        frames.append(frame.astype(np.uint16))
    out = frame_intensity(frames, full_scale=4095)
    assert out["rheed_background"] == 100 and out["rheed_signal"] == pytest.approx(900, rel=1e-3)
    assert out["rheed_noise"] == pytest.approx(0.02, rel=0.01)
    assert (out["rheed_full_scale"], out["rheed_clipped_px"]) == (4095, 0)
    assert out["rheed_headroom"] == round(1018 / 4095, 4)

    # Moved somewhere else: the same numbers. No mask, no positions.
    moved = [np.roll(f, (37, 11), axis=(0, 1)) for f in frames]
    assert frame_intensity(moved, full_scale=4095) == out

    clipped = np.full((10, 10), 100, dtype=np.uint16)
    clipped[0, :5] = 4095
    out = frame_intensity([clipped], full_scale=4095)
    assert (out["rheed_clipped_px"], out["rheed_headroom"], out["rheed_noise"]) == (5, 1.0, None)
    # Short exposure: the image never comes near full scale, and says how near it is.
    dim = frame_intensity([np.full((4, 4), 300, dtype=np.uint16)], full_scale=4095)
    assert (dim["rheed_clipped_px"], dim["rheed_headroom"]) == (0, round(300 / 4095, 4))
    # A camera that does not say: uint8 is 255; wider is unknown, not guessed.
    assert frame_intensity([np.full((4, 4), 255, dtype=np.uint8)])["rheed_clipped_px"] == 16
    unknown = frame_intensity([np.full((4, 4), 300, dtype=np.uint16)])
    assert (unknown["rheed_full_scale"], unknown["rheed_headroom"], unknown["rheed_clipped_px"]) == (None, None, None)
    assert frame_intensity([]) is None
