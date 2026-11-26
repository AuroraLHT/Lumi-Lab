"""Switching the RHEED gun on and off at the lab's operating values (pld-service only).

pld-service ramps every gun set point itself (filament 30 mA/s, HV 500 V/s, focus and
deflection in 0.2 s steps); this decides the order and the values, and checks the
readbacks got there:

1. filament on at `filament_start_ma`,
2. HV on at `hv_kv`,
3. the filament up in `filament_step_ma` steps, to `filament_max_ma` at most, until the
   beam has saturated. Who decides is the `judge`:

   - "agent" (the lab's default): the calling agent, from the emission current and the
     RHEED image's intensity together -- both level off when the beam saturates.
     `on` opens the search, the agent calls `step_up` per step and `finish` at the step
     it chooses; no confirmation is asked of anyone.
   - "person": the same steps, but behind a `rheed_emission` pending confirmation, for
     a person who reads the table and says up or stop.
   - "rule": by itself, on the emission alone. Saturated when a step leaves its first
     `emission_digits` significant digits unchanged (the lab's rule: 8.41 -> 8.47 uA is
     saturated, 8.41 -> 8.52 is not); that step is taken back, so the filament runs no
     hotter than it needs to.

   Each step reads the emission and, when there is a RHEED camera, the image's pixel
   statistics (`frame_intensity`: the signal of its brightest pixels above background,
   its noise, how near full scale it is); `saturation_hint` puts the two side by side. The hint is not the decision,
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


#: The brightest share of an image's pixels whose mean is its signal (see frame_intensity).
#: 10%, not 1%: on the twin's STO frame a third of the brightest 1% already sits at full
#: scale, so that mean stops rising with the beam; of the brightest 10%, 3.5% does.
BRIGHT_FRACTION = 0.10


@dataclass(frozen=True)
class RheedGunSettings:
    """The gun's operating values (`settings.experiment.rheed`), and how close a
    readback has to come and how long it may take."""

    hv_kv: float = 25.0
    filament_start_ma: float = 1800.0
    filament_max_ma: float = 2100.0
    filament_step_ma: float = 50.0
    #: judge "rule": saturated when a step leaves this many leading digits of the
    #: emission unchanged (the lab's rule; the hint for agent and person uses the plateau).
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
    #: rheed_on's default judge of saturation: "agent", "person" or "rule" (see above).
    filament_judge: str = "agent"
    #: Saturated when the emission and the RHEED signal are each within this fraction of
    #: the plateau their steps level off to (`plateau_estimate`): 0.1 stops at 90% of it,
    #: the last 10% of brightness not being worth the filament.
    plateau_fraction: float = 0.10
    #: While a plateau cannot be estimated yet (fewer than 3 steps, not levelling off):
    #: a step that changes the emission by less than this fraction leaves it flat.
    emission_flat_fraction: float = 0.01
    #: The same for the RHEED signal (or twice its frame-to-frame noise, if larger).
    intensity_flat_fraction: float = 0.03
    #: Frames read for one intensity reading: their mean, and their scatter as the noise.
    intensity_frames: int = 5
    #: The brightest share of the image's pixels whose mean is the RHEED signal.
    bright_fraction: float = BRIGHT_FRACTION

    def with_overrides(self, **values) -> "RheedGunSettings":
        return replace(self, **{k: float(v) for k, v in values.items() if v is not None})

    @classmethod
    def from_settings(cls, values: dict) -> "RheedGunSettings":
        """`[experiment.rheed]`; a key left out keeps the default."""
        fields = {f.name: f for f in dataclasses.fields(cls)}
        if unknown := sorted(set(values) - set(fields)):
            raise RuntimeError(f"[experiment.rheed] has unknown keys: {', '.join(unknown)}")
        def cast(k, v):
            kind = fields[k].type
            return (bool(v) if kind in (bool, "bool") else str(v) if kind in (str, "str")
                    else float(v))
        made = cls(**{k: cast(k, v) for k, v in values.items()})
        check_judge(made.filament_judge)
        return made


JUDGES = ("agent", "person", "rule")


def check_judge(judge: str) -> str:
    if judge not in JUDGES:
        raise ValueError(f"judge {judge!r} is not one of {', '.join(JUDGES)}")
    return judge


def frame_intensity(frames: list, full_scale: int | None = None,
                    bright_fraction: float = BRIGHT_FRACTION) -> dict | None:
    """Pixel statistics of the RHEED image over a few frames, for any pattern: nothing
    here knows where the spots are, only how the pixel values are spread.

    - rheed_background: the median pixel, the screen between the features.
    - rheed_signal: the mean of the brightest `bright_fraction` of pixels, less the
      background. Whatever the pattern, its spots and streaks are those pixels; this is
      what the emission search compares.
    - rheed_noise: the signal's frame-to-frame spread over its mean (None from one
      frame). A change smaller than about twice this is not a change.
    - rheed_p999, rheed_max: the top of the image.
    - rheed_full_scale: the largest value a pixel can take (the camera's, else from the
      dtype: 255 for uint8, None for anything wider it does not report).
    - rheed_headroom: rheed_max over full scale (1 = the top is at full scale).
    - rheed_clipped_px: pixels at full scale in any of the frames, so one clipped pixel
      in one frame counts (an average over the frames rounded a few away to 0). A short
      exposure may never reach it; then this is 0 and only the headroom says how close
      it is.
    - rheed_bright_fraction: the `bright_fraction` the signal was taken over.
    - rheed_mean: the whole image's mean, for comparison with older records.
    """
    import numpy as np

    frames = [np.asarray(f) for f in frames if f is not None and np.size(f)]
    if not frames:
        return None
    if full_scale is None and frames[0].dtype == np.uint8:
        full_scale = 255
    stack = np.stack(frames).astype(float).reshape(len(frames), -1)
    n_bright = max(1, int(round(stack.shape[1] * bright_fraction)))
    background = np.median(stack, axis=1)
    bright = np.partition(stack, -n_bright, axis=1)[:, -n_bright:].mean(axis=1)
    signal = bright - background
    mean_signal = float(signal.mean())
    noise = (float(signal.std(ddof=1)) / mean_signal
             if len(frames) > 1 and mean_signal > 0 else None)
    peak = float(stack.max())
    out = {
        "rheed_background": round(float(background.mean()), 3),
        "rheed_signal": round(mean_signal, 3),
        "rheed_noise": None if noise is None else round(noise, 5),
        "rheed_p999": round(float(np.percentile(stack, 99.9)), 3),
        "rheed_max": round(peak, 3),
        "rheed_full_scale": full_scale,
        "rheed_headroom": round(peak / full_scale, 4) if full_scale else None,
        "rheed_clipped_px": (int((stack >= full_scale).any(axis=0).sum())
                             if full_scale else None),
        "rheed_bright_fraction": bright_fraction,
        "rheed_mean": round(float(stack.mean()), 3),
    }
    return out


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


#: Shrink ratios tried when fitting a plateau: one step's rise over the step before's.
_RATIOS = tuple(i / 200 for i in range(2, 196))


def plateau_estimate(values: list[float], noise: float = 0.0) -> tuple[float, float] | None:
    """(plateau, last value over it) for readings that level off exponentially, one per
    filament step, or None while they do not (yet).

    A saturating reading y = P - B r^k, k the step: each step adds r times what the step
    before added. Fitted over every step (r on a grid, P and B by least squares for
    each), so one noisy step does not decide it. None with fewer than 3 steps, a best r
    near 1 (rising steadily, no plateau in sight), or a total rise within `noise`
    (relative, the readings' own scatter) of the last value."""
    import numpy as np

    y = np.asarray(values, dtype=float)
    if len(y) < 3 or not np.all(np.isfinite(y)) or y[-1] <= 0:
        return None
    if y[-1] - y[0] <= 2 * noise * y[-1]:
        return None
    k = np.arange(len(y), dtype=float)
    best = None
    for r in _RATIOS:
        design = np.column_stack([np.ones_like(k), -(r ** k)])
        (p, b), *_ = np.linalg.lstsq(design, y, rcond=None)
        sse = float(np.sum((design @ (p, b) - y) ** 2))
        if b > 0 and (best is None or sse < best[0]):
            best = (sse, r, float(p))
    if best is None or best[1] >= _RATIOS[-1] or best[2] <= 0:
        return None
    plateau = max(best[2], float(y[-1]))
    return plateau, float(y[-1]) / plateau


def _judge(name: str, values: list, unit: str, plateau_fraction: float, step_fraction: float,
           noise: float = 0.0) -> tuple[bool | None, str, int | None]:
    """One reading's verdict, from its plateau if one can be estimated, else from the
    last step's change: (flat, why, first) with `first` the index of the first step
    already within `plateau_fraction` of the plateau (None without one). flat None:
    nothing to judge."""
    if len(values) < 2 or values[-1] is None or values[-2] is None or values[-2] <= 0:
        return None, f"no {name} to compare", None
    known = []
    for v in reversed(values):  # the run of readings up to the last one
        if v is None:
            break
        known.append(v)
    known.reverse()
    a, b = values[-2], values[-1]
    change = (b - a) / a
    band = max(step_fraction, 2 * noise)
    step_flat = abs(change) < band
    within = f"{band:.1%}" + (" (2x the frame-to-frame noise)" if band > step_fraction else "")
    step = f"{a:.4g} -> {b:.4g}{unit}, {change:+.1%}"
    estimate = plateau_estimate(known, noise)
    if estimate is not None:
        plateau, reached = estimate
        if reached >= 1 - plateau_fraction:
            offset = len(values) - len(known)
            first = next(offset + i for i, v in enumerate(known) if v >= (1 - plateau_fraction) * plateau)
            return True, (f"{name} at {reached:.0%} of its plateau (~{plateau:.4g}{unit}); within "
                          f"{plateau_fraction:.0%} of it"), first
        if step_flat:
            # A step that added nothing is flat, whatever the fit says is still to come:
            # the readings need not be an exponential.
            return True, (f"{name} flat ({step}, under {within}), though the fit puts it at "
                          f"{reached:.0%} of a plateau of ~{plateau:.4g}{unit}"), len(values) - 2
        return False, (f"{name} at {reached:.0%} of its plateau (~{plateau:.4g}{unit}); not yet "
                       f"within {plateau_fraction:.0%} of it ({step})"), None
    return step_flat, (f"{name} {'flat' if step_flat else 'rising' if change > 0 else 'falling'} "
                       f"({step}; no plateau estimate yet, so a step under {within} is flat)"), \
        (len(values) - 2 if step_flat else None)


def saturation_hint(steps: list[dict], s: "RheedGunSettings") -> str:
    """For whoever decides: how near the emission and the RHEED signal are to the
    plateau each levels off to (within `plateau_fraction` of it is saturated), or,
    before a plateau can be estimated, what the last step changed. Both must agree.

    A clipped image is not judged: pixels at full scale cannot rise, so its signal
    levels off before the beam does. At `filament_max_ma` there is no step left to
    offer, only where to stop."""
    if len(steps) < 2:
        return "step up for the next step, or stop"
    prev, last = steps[-2], steps[-1]
    e_flat, emission, e_first = _judge("emission", [st.get("emission_ua") for st in steps], " uA",
                              s.plateau_fraction, s.emission_flat_fraction)
    parts = [emission]
    if last.get("rheed_clipped_px"):
        # The gain is a person's call (rheed_gain), whoever judges the search: say what
        # the clipping means and who can change it, not "lower it".
        i_flat, i_first = None, None
        parts.append(f"RHEED image not judged: {last['rheed_clipped_px']} pixels at full scale "
                     "cannot rise, so its signal levels off before the beam does; the emission "
                     "decides. If the image matters, a person can lower the gain or exposure "
                     "(begin_adjust_rheed_gain asks them)")
    else:
        noise = max(prev.get("rheed_noise") or 0.0, last.get("rheed_noise") or 0.0)
        i_flat, intensity, i_first = _judge("RHEED signal", [st.get("rheed_signal") for st in steps], "",
                                   s.plateau_fraction, s.intensity_flat_fraction, noise)
        parts.append(intensity)
    if last.get("rheed_headroom") is not None:
        parts.append(f"image peak at {last['rheed_headroom']:.0%} of full scale")
    # The lowest step that already has both within the fraction (by the step rule: the
    # step before the flat one).
    firsts = [f for f in (e_first, i_first) if f is not None]
    at = steps[max(firsts)] if firsts else prev
    stop = f"stop at {at['filament_ma']:g} mA?"
    if e_flat and i_flat in (True, None) or i_flat and e_flat is None:
        verdict = f"saturated: {stop}"
    elif s.filament_max_ma is not None and last["filament_ma"] >= s.filament_max_ma:
        here = f"at the filament cap ({s.filament_max_ma:g} mA), no step left"
        if e_flat is None and i_flat is None:
            verdict = f"{here}: nothing to compare, stop here and judge by eye"
        else:
            verdict = (f"{here}: {'the two disagree' if e_flat or i_flat else 'still rising'}; stop at "
                       f"{last['filament_ma']:g} mA, or back at {prev['filament_ma']:g} mA?")
    elif e_flat or i_flat:
        verdict = f"the two disagree: one more step, or {stop}"
    elif e_flat is None and i_flat is None:
        verdict = "nothing to compare: judge by eye"
    else:
        verdict = "still rising"
    return f"{verdict} ({'; '.join(parts)})"


def emission_table(steps: list[dict]) -> str:
    """The emission search as a table: filament current against emission, and with a
    camera the RHEED signal above background, its noise and the peak's share of full
    scale."""
    def cell(v, fmt="{:.4g}"):
        return "-" if v is None else fmt.format(v)
    images = any(step.get("rheed_signal") is not None for step in steps)
    head = "filament mA | emission uA"
    if images:
        head += " | RHEED signal | noise | peak/full"
    rows = [head]
    for step in steps:
        row = f"{step['filament_ma']:>11g} | {cell(step['emission_ua'])}"
        if images:
            row = f"{row:<25}" + (f" | {cell(step.get('rheed_signal')):>12} | {cell(step.get('rheed_noise'), '{:.1%}'):>5}"
                    f" | {cell(step.get('rheed_headroom'), '{:.0%}'):>9}")
        rows.append(row)
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

    def __init__(self, chamber, settings: RheedGunSettings, intensity=None) -> None:
        self.chamber = chamber
        self.settings = settings
        #: async (n_frames) -> `frame_intensity` dict or None: the RHEED image's pixel
        #: statistics. None: no camera, the steps carry the emission only.
        self.intensity = intensity
        self._lock = asyncio.Lock()
        #: The emission search in progress (judge "agent" or "person"): its settings,
        #: judge and steps.
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
        """An emission search is open (`on` with judge "agent"/"person" until `finish`)."""
        return self._search is not None

    def search_view(self) -> dict | None:
        """The open search for a UI (`RheedSearch`), or None."""
        if self._search is None:
            return None
        s, steps = self._search["settings"], self._search["steps"]
        return {"judge": self._search["judge"], "started_at": self._search["started_at"],
                "steps": [dict(step) for step in steps], "filament_max_ma": s.filament_max_ma,
                "saturation": saturation_hint(steps, s)}

    @property
    def judge(self) -> str | None:
        """Who judges the open search, or None."""
        return None if self._search is None else self._search["judge"]

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

    async def on(self, judge: str = "rule", **overrides) -> dict:
        """Filament and HV on, then the emission search: by itself (judge "rule") to
        saturation and on to the optics, or one step at a time for an agent or a person
        (judge "agent"/"person"), who calls `step_up` and then `finish`."""
        check_judge(judge)
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
            steps = [{"filament_ma": filament, **await self._reading(s)}]
            if judge != "rule":
                self._search = {"settings": s, "judge": judge, "steps": steps,
                                "started_at": time.time()}
                return self._search_report(f"filament and HV on; the emission search waits for the {judge}")
            filament, saturation = await self._saturate(s, steps)
            return await self._optics(where, s, filament, steps, saturation)

    async def step_up(self) -> dict:
        """The judge's next step: the filament up one `filament_step_ma`, and its
        emission and RHEED intensity."""
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
            steps.append({"filament_ma": nxt, **await self._reading(s)})
        return self._search_report(f"filament up to {nxt:g} mA")

    async def finish(self, filament_ma: float | None = None) -> dict:
        """End the search at `filament_ma` (one of the tried steps; None = where it is
        now), then focus and deflection."""
        search = self._require_search()
        s, steps, judge = search["settings"], search["steps"], search["judge"]
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
                                      f"the {judge} stopped the search at {target:g} mA", judge)

    def cancel_search(self) -> None:
        self._search = None

    def _require_search(self) -> dict:
        if self._search is None:
            raise RuntimeError("no emission search is open: rheed_on (judge agent or person) first")
        return self._search

    def _search_report(self, what: str) -> dict:
        s, steps, judge = self._search["settings"], self._search["steps"], self._search["judge"]
        hint = saturation_hint(steps, s)
        return {"filament_ma": steps[-1]["filament_ma"], "emission_steps": list(steps),
                "saturation": hint, "settings": asdict(s), "searching": True, "judge": judge,
                "message": f"{what}. {hint}\n{emission_table(steps)}"}

    async def _optics(self, where: dict, s: RheedGunSettings, filament: float,
                      steps: list[dict], saturation: str, judge: str = "rule") -> dict:
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
                "judge": judge,
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

    async def _reading(self, s: RheedGunSettings) -> dict:
        """One step's reading after the settle time: the emission, and the RHEED
        intensity when there is a camera (a camera that fails leaves it out)."""
        await asyncio.sleep(s.emission_settle_s)
        reading = {"emission_ua": (await self._rheed()).emission_ua}
        if self.intensity is not None:
            try:
                reading.update(await self.intensity(int(s.intensity_frames),
                                                    bright_fraction=s.bright_fraction) or {})
            except Exception as exc:  # the camera is a second opinion, not a step
                log.warning("RHEED intensity not read: %s", exc)
        return reading

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
            steps.append({"filament_ma": nxt, **await self._reading(s)})
            after = steps[-1]["emission_ua"]
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
