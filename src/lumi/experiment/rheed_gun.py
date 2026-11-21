"""Switching the RHEED gun on and off at the lab's operating values (pld-service only).

pld-service ramps every gun set point itself (filament 30 mA/s, HV 500 V/s, focus and
deflection in 0.2 s steps); this decides the order and the values, and checks the
readbacks got there:

1. filament on at `filament_start_ma`,
2. HV on at `hv_kv`,
3. the filament up in `filament_step_ma` steps while the emission current still rises
   -- by itself, or one step at a time for a person who says when to stop
   (`by_person`: `on`, then `step_up` per step, then `finish`) --
   to `filament_max_ma` at most. The emission is saturated when a step leaves its first
   `emission_digits` significant digits unchanged (the lab's rule: 8.41 -> 8.47 uA is
   saturated, 8.41 -> 8.52 is not). That step is taken back, so the filament runs no
   hotter than it needs to,
4. focus, then deflection X and Y.

The values are the lab's (`[experiment.rheed]`), not the gun's remembered set points:
those are whatever the last person or test left. Focus and deflection depend on the
alignment, so a call can override any of them. Beam-position screws, the incidence
angle and the camera gain stay with a person.

A step that does not get there raises with what was and was not reached, and leaves
the gun as it got: switching it off is a separate decision (`rheed_off`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
import dataclasses
from dataclasses import asdict, dataclass, replace

from lumi.contracts.payloads.common import Empty
from lumi.contracts.payloads.pld import (
    PldRheed,
    PldRheedDeflectionRequest,
    PldRheedFilamentOnRequest,
    PldRheedFilamentRequest,
    PldRheedFocusRequest,
    PldRheedHvOnRequest,
    PldRheedHvRequest,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RheedGunSettings:
    """The gun's operating values (`settings.experiment.rheed`), and how close a
    readback has to come and how long it may take."""

    hv_kv: float = 25.0
    filament_start_ma: float = 1800.0
    filament_max_ma: float = 2100.0
    filament_step_ma: float = 50.0
    #: Saturated when a step leaves this many leading digits of the emission unchanged.
    emission_digits: int = 2
    #: Seconds after a filament step before the emission is read.
    emission_settle_s: float = 3.0
    focus_ma: float = 476.0
    deflection_x_ma: float = -61.0
    deflection_y_ma: float = 223.0
    filament_tolerance_ma: float = 20.0
    hv_tolerance_kv: float = 0.5
    optics_tolerance_ma: float = 5.0
    #: How long one ramp (filament, HV) may take to arrive.
    ramp_timeout_s: float = 240.0
    poll_s: float = 0.5
    #: rheed_on's default: a person says when the emission has saturated (step_up /
    #: finish), rather than the digit rule deciding.
    filament_by_person: bool = True

    def with_overrides(self, **values) -> "RheedGunSettings":
        return replace(self, **{k: float(v) for k, v in values.items() if v is not None})

    @classmethod
    def from_settings(cls, values: dict) -> "RheedGunSettings":
        """`[experiment.rheed]`; a key left out keeps the default."""
        fields = {f.name: f for f in dataclasses.fields(cls)}
        if unknown := sorted(set(values) - set(fields)):
            raise RuntimeError(f"[experiment.rheed] has unknown keys: {', '.join(unknown)}")
        return cls(**{k: bool(v) if fields[k].type in (bool, "bool") else float(v)
                      for k, v in values.items()})


def leading_digits(value: float, n: int) -> tuple[int, int]:
    """The first `n` significant digits of a positive value, as read off a display
    (truncated, not rounded), with its decade: 8.6652 -> (0, 86), 12.3 -> (1, 12)."""
    exponent = math.floor(math.log10(value))
    return exponent, math.floor(value / 10 ** (exponent - n + 1) + 1e-9)


def saturated(before: float | None, after: float | None, digits: int) -> bool:
    """The lab's rule: a filament step that leaves the emission's first `digits`
    significant digits unchanged (or did not raise it) has saturated it."""
    if before is None or after is None or before <= 0 or after <= 0:
        return False
    return leading_digits(after, digits) <= leading_digits(before, digits)


def saturation_hint(steps: list[dict], digits: int) -> str:
    """For the person deciding: whether the last step saturated, by the lab's rule."""
    if len(steps) < 2:
        return "say up for the next step, or stop"
    a, b = steps[-2]["emission_ua"], steps[-1]["emission_ua"]
    if a is None or b is None:
        return "no emission reading to compare: judge by eye"
    if saturated(a, b, digits):
        return (f"the first {digits} digits did not change ({a:.4g} -> {b:.4g} uA): saturated by "
                f"the lab's rule, stop at {steps[-2]['filament_ma']:g} mA?")
    return f"still rising ({a:.4g} -> {b:.4g} uA)"


def emission_table(steps: list[dict]) -> str:
    """The emission search as a table: filament current against emission."""
    rows = ["filament mA | emission uA"]
    for step in steps:
        e = step["emission_ua"]
        rows.append(f"{step['filament_ma']:>11g} | {'-' if e is None else f'{e:.4g}'}")
    return "\n".join(rows)


def gun_summary(r: PldRheed) -> dict:
    """The gun as it reads now: what `rheed_on`/`rheed_off` report and record."""
    return {
        "filament_on": r.filament_on, "filament_ma": r.filament_readback_ma,
        "filament_set_ma": r.filament_set_ma,
        "hv_on": r.hv_on, "hv_kv": r.hv_readback_kv, "hv_set_kv": r.hv_set_kv,
        "emission_ua": r.emission_ua,
        "focus_on": r.focus_on, "focus_ma": r.focus_readback_ma,
        "x_on": r.x_on, "x_ma": r.x_readback_ma, "y_on": r.y_on, "y_ma": r.y_readback_ma,
    }


def _brief(r: PldRheed) -> str:
    def f(v, unit):
        return "?" if v is None else f"{v:g} {unit}"
    return (f"filament {'on' if r.filament_on else 'off'} {f(r.filament_readback_ma, 'mA')}, "
            f"HV {'on' if r.hv_on else 'off'} {f(r.hv_readback_kv, 'kV')}, "
            f"emission {f(r.emission_ua, 'uA')}, focus {f(r.focus_readback_ma, 'mA')}, "
            f"X {f(r.x_readback_ma, 'mA')}, Y {f(r.y_readback_ma, 'mA')}")


def _close(readback: float | None, target: float, tolerance: float) -> bool:
    return readback is not None and abs(readback - target) <= tolerance


class RheedGun:
    """`rheed_on` / `rheed_off` on a `PldChamber`."""

    def __init__(self, chamber, settings: RheedGunSettings) -> None:
        self.chamber = chamber
        self.settings = settings
        self._lock = asyncio.Lock()
        #: The person-driven emission search in progress: its settings and steps.
        self._search: dict | None = None

    async def _rheed(self) -> PldRheed:
        return (await self.chamber.state()).rheed

    async def _wait(self, what: str, done, timeout: float, poll: float) -> PldRheed:
        deadline = time.monotonic() + timeout
        while True:
            r = await self._rheed()
            if done(r):
                return r
            if r.interlock or r.filament_open:
                raise RuntimeError(f"{what}: the gun switched itself off "
                                   f"({'interlock' if r.interlock else 'open filament'})")
            if time.monotonic() > deadline:
                raise RuntimeError(f"{what}: not there after {timeout:g} s")
            await asyncio.sleep(poll)

    def _refuse(self, r: PldRheed) -> str | None:
        if not r.present:
            return "pld-service has no RHEED link"
        if r.read_only:
            return "pld-service's RHEED port is read-only (hardware without --allow-operations)"
        if not r.link_ok:
            return f"the RHEED controller did not answer its last poll ({r.last_alert or 'no alert'})"
        if r.interlock:
            return "the RHEED controller's interlock is set: a person has to clear it"
        if r.filament_open:
            return "the RHEED filament reads open (burnt or broken): a person has to look at it"
        if r.filament_ramping_down or r.hv_ramping_down or r.all_ramping_down:
            return "the gun is ramping down: wait for it to finish"
        return None

    @property
    def searching(self) -> bool:
        """A person-driven emission search is open (`on(by_person=True)` until `finish`)."""
        return self._search is not None

    def _check(self, r: PldRheed, s: RheedGunSettings) -> None:
        if (why := self._refuse(r)) is not None:
            raise RuntimeError(f"rheed_on refused: {why}")
        lo, hi = r.filament_min_ma, r.filament_max_ma
        if not s.filament_start_ma <= s.filament_max_ma:
            raise ValueError(f"filament_start_ma {s.filament_start_ma:g} is above "
                             f"filament_max_ma {s.filament_max_ma:g}")
        if hi is not None and s.filament_max_ma > hi:
            raise ValueError(f"filament_max_ma {s.filament_max_ma:g} mA is above the gun's "
                             f"limit, {hi:g} mA")
        if lo is not None and s.filament_start_ma < lo:
            raise ValueError(f"filament_start_ma {s.filament_start_ma:g} mA is below the "
                             f"gun's floor, {lo:g} mA")
        if r.hv_max_kv is not None and s.hv_kv > r.hv_max_kv:
            raise ValueError(f"hv_kv {s.hv_kv:g} is above the gun's limit, {r.hv_max_kv:g} kV")

    @contextlib.asynccontextmanager
    async def _stage(self):
        """Run gun steps; a failure says where it stopped and what the gun reads."""
        if self._lock.locked():
            raise RuntimeError("rheed_on or rheed_off is already running")
        async with self._lock:
            where = {"stage": "start"}
            try:
                yield where
            except Exception as exc:
                if where["stage"] == "start":  # refused before anything moved
                    raise
                now = await self._safe_rheed()
                raise RuntimeError(
                    f"rheed_on stopped at the {where['stage']} step: {exc}. The gun is left as it is"
                    + (f": {_brief(now)}" if now is not None else "")) from exc

    async def on(self, by_person: bool = False, **overrides) -> dict:
        """Filament and HV on, then the emission search: by itself (`by_person` false) to
        saturation and on to the optics, or one step at a time for a person, who calls
        `step_up` and then `finish`."""
        if self._search is not None:
            raise RuntimeError("an emission search is already open: step_up or finish it")
        s = self.settings.with_overrides(**overrides)
        async with self._stage() as where:
            r = await self._rheed()
            self._check(r, s)
            where["stage"] = "filament"
            filament = await self._filament(s, s.filament_start_ma, r.filament_on)
            where["stage"] = "HV"
            if (await self._rheed()).hv_on:
                await self.chamber._call("set_rheed_hv", PldRheedHvRequest(kv=s.hv_kv))
            else:
                await self.chamber._call("rheed_hv_on", PldRheedHvOnRequest(kv=s.hv_kv))
            await self._wait(
                f"HV to {s.hv_kv:g} kV",
                lambda r: r.hv_on and _close(r.hv_set_kv, s.hv_kv, 0.01)
                and _close(r.hv_readback_kv, s.hv_kv, s.hv_tolerance_kv),
                s.ramp_timeout_s, s.poll_s)
            where["stage"] = "emission"
            steps = [{"filament_ma": filament, "emission_ua": await self._emission(s)}]
            if by_person:
                self._search = {"settings": s, "steps": steps}
                return self._search_report("filament and HV on; the emission search waits for the person")
            filament, saturation = await self._saturate(s, steps)
            return await self._optics(where, s, filament, steps, saturation)

    async def step_up(self) -> dict:
        """The person's next step: the filament up one `filament_step_ma`, and its emission."""
        search = self._require_search()
        s, steps = search["settings"], search["steps"]
        filament = steps[-1]["filament_ma"]
        nxt = min(filament + s.filament_step_ma, s.filament_max_ma)
        if nxt <= filament:
            return self._search_report(f"the filament is at its maximum, {filament:g} mA: finish here "
                                       "or at an earlier step")
        async with self._stage() as where:
            where["stage"] = "emission"
            await self._filament(s, nxt, True)
            steps.append({"filament_ma": nxt, "emission_ua": await self._emission(s)})
        return self._search_report(f"filament up to {nxt:g} mA")

    async def finish(self, filament_ma: float | None = None) -> dict:
        """End the person's search at `filament_ma` (one of the tried steps; None = where
        it is now), then focus and deflection."""
        search = self._require_search()
        s, steps = search["settings"], search["steps"]
        tried = [step["filament_ma"] for step in steps]
        target = tried[-1] if filament_ma is None else float(filament_ma)
        if target not in tried:
            raise ValueError(f"filament_ma {target:g} is not one of the tried steps "
                             f"({', '.join(f'{t:g}' for t in tried)})")
        async with self._stage() as where:
            where["stage"] = "emission"
            if target != tried[-1]:
                await self._filament(s, target, True)
            self._search = None
            return await self._optics(where, s, target, steps,
                                      f"the person stopped the search at {target:g} mA")

    def cancel_search(self) -> None:
        self._search = None

    def _require_search(self) -> dict:
        if self._search is None:
            raise RuntimeError("no emission search is open: rheed_on with by_person first")
        return self._search

    def _search_report(self, what: str) -> dict:
        s, steps = self._search["settings"], self._search["steps"]
        hint = saturation_hint(steps, int(s.emission_digits))
        return {"filament_ma": steps[-1]["filament_ma"], "emission_steps": list(steps),
                "saturation": hint, "settings": asdict(s), "searching": True,
                "message": f"{what}. {hint}\n{emission_table(steps)}"}

    async def _optics(self, where: dict, s: RheedGunSettings, filament: float,
                      steps: list[dict], saturation: str) -> dict:
        where["stage"] = "focus"
        await self.chamber._call("set_rheed_focus", PldRheedFocusRequest(ma=s.focus_ma))
        where["stage"] = "deflection"
        await self.chamber._call("set_rheed_deflection", PldRheedDeflectionRequest(
            x_ma=s.deflection_x_ma, y_ma=s.deflection_y_ma))
        where["stage"] = "optics readback"
        await self._wait(
            "focus and deflection",
            lambda r: r.focus_on and r.x_on and r.y_on
            and _close(r.focus_readback_ma, s.focus_ma, s.optics_tolerance_ma)
            and _close(r.x_readback_ma, s.deflection_x_ma, s.optics_tolerance_ma)
            and _close(r.y_readback_ma, s.deflection_y_ma, s.optics_tolerance_ma),
            10.0, s.poll_s)
        r = await self._rheed()
        log.info("RHEED gun on: %s (%s)", _brief(r), saturation)
        return {"gun": gun_summary(r), "filament_ma": filament, "emission_steps": steps,
                "saturation": saturation, "settings": asdict(s), "searching": False,
                "message": f"RHEED gun on: {_brief(r)}; {saturation}\n{emission_table(steps)}"}

    async def _safe_rheed(self) -> PldRheed | None:
        try:
            return await self._rheed()
        except Exception:
            return None

    async def _filament(self, s: RheedGunSettings, ma: float, is_on: bool) -> float:
        if is_on:
            await self.chamber._call("set_rheed_filament", PldRheedFilamentRequest(ma=ma))
        else:
            await self.chamber._call("rheed_filament_on", PldRheedFilamentOnRequest(ma=ma))
        await self._wait(
            f"filament to {ma:g} mA",
            lambda r: r.filament_on and _close(r.filament_set_ma, ma, 0.5)
            and _close(r.filament_readback_ma if r.filament_readback_ma is not None else r.filament_set_ma,
                       ma, s.filament_tolerance_ma),
            s.ramp_timeout_s, s.poll_s)
        return ma

    async def _emission(self, s: RheedGunSettings) -> float | None:
        await asyncio.sleep(s.emission_settle_s)
        return (await self._rheed()).emission_ua

    async def _saturate(self, s: RheedGunSettings, steps: list[dict]) -> tuple[float, str]:
        """Raise the filament while the emission's leading digits still change; take back
        the step that left them as they were."""
        filament, emission = steps[-1]["filament_ma"], steps[-1]["emission_ua"]
        if emission is None or emission <= 0:
            return filament, (f"no emission reading: the filament stays at {filament:g} mA, "
                              "check the emission by eye")
        while True:
            nxt = min(filament + s.filament_step_ma, s.filament_max_ma)
            if nxt <= filament:
                return filament, (f"filament at its maximum, {filament:g} mA, emission "
                                  f"{emission:.4g} uA, still rising")
            await self._filament(s, nxt, True)
            after = await self._emission(s)
            steps.append({"filament_ma": nxt, "emission_ua": after})
            if after is None or after <= 0 or saturated(emission, after, int(s.emission_digits)):
                await self._filament(s, filament, True)
                return filament, (f"emission saturated at {emission:.4g} uA: filament "
                                  f"{filament:g} mA (+{s.filament_step_ma:g} mA gave "
                                  f"{'no reading' if after is None else f'{after:.4g} uA'})")
            filament, emission = nxt, after

    async def off(self) -> dict:
        if self._lock.locked():
            raise RuntimeError("rheed_on or rheed_off is already running")
        async with self._lock:
            r = await self._rheed()
            if not r.present:
                raise RuntimeError("rheed_off refused: pld-service has no RHEED link")
            self._search = None
            await self.chamber._call("rheed_all_off_gentle", Empty())
            r = await self._wait("filament and HV off", lambda r: not r.filament_on and not r.hv_on,
                                 30.0, self.settings.poll_s)
            log.info("RHEED gun off: %s", _brief(r))
            return {"gun": gun_summary(r),
                    "message": f"RHEED gun off: {_brief(r)}. Focus, X and Y stay as pld-service "
                               "leaves them"}
