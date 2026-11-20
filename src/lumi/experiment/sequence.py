"""Step lists for pld-service's RunSequence, as Lumi stores and vets them.

A step is a pld.proto `SequenceStep` in proto JSON names -- `{"mask": {"position": 20,
"wait": false, "speed_mm_s": 0.5}}`, `{"loop_start": 3}`, `{"pause": "check the film"}`
-- with an optional "label". pld-service checks every step again when it runs, with the
same rules as calling the operation directly; what is checked here is what Lumi adds:

- **`meter_energy`** (`{"mj": 120}`) is Lumi's own step: the pulse energy wanted at the
  meter. At run time it becomes pld-service's `laser_energy` at mj / today's laser-test
  ratio, as `begin_set_laser_power` sets it. A raw `laser_energy` (a source energy) is
  refused, so a stored list never bypasses the daily test.
- The shape: one operation per step, known fields, matching loops, at most
  `MAX_OPERATIONS` operations with the loops multiplied out (pld-service's own limit).

The canonical form (keys sorted, no spaces) is what is stored and hashed, so a version's
hash names exactly the steps a person approved.
"""

from __future__ import annotations

import hashlib
import re
import json

from google.protobuf import json_format

from lumi.contracts.payloads.pld import LASER_RATE_MAX_HZ
from lumi.pascal.pld.proto import pld_pb2 as pb

MAX_STEPS = 10_000
MAX_OPERATIONS = 1_000_000

#: Steps that fire the laser, and Lumi's step that sets its energy.
FIRE = "fire_laser"
METER_ENERGY = "meter_energy"

_OPS = {f.name for f in pb.SequenceStep.DESCRIPTOR.oneofs_by_name["op"].fields}
_LUMI_OPS = {METER_ENERGY}


def _op(step: dict) -> str:
    ops = [k for k in step if k != "label"]
    if len(ops) != 1:
        raise ValueError(f"a step is one operation (and an optional label), got {sorted(step)}")
    return ops[0]


def check(steps: list[dict]) -> dict:
    """Vet a step list. Returns its canonical JSON, hash, and what it does; raises
    ValueError naming the first bad step."""
    if not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"a sequence has 1..{MAX_STEPS} steps, got {len(steps)}")
    steps = list(steps)
    proto_steps = []
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise ValueError(f"step {i}: a step is an object, got {step!r}")
        try:
            op = _op(step)
        except ValueError as e:
            raise ValueError(f"step {i}: {e}") from None
        if op == "laser_energy":
            raise ValueError(
                f"step {i}: laser_energy sets the excimer's own energy; use "
                "{\"meter_energy\": {\"mj\": ...}}, the energy at the meter, which Lumi sets from "
                "today's laser test")
        if op not in _OPS | _LUMI_OPS:
            raise ValueError(f"step {i}: unknown operation {op!r} (one of {sorted(_OPS - {'laser_energy'} | _LUMI_OPS)})")
        if op == FIRE and isinstance(step[op], dict) and isinstance(step[op].get("rate_hz"), (int, float)) \
                and step[op]["rate_hz"] > LASER_RATE_MAX_HZ:
            raise ValueError(f"step {i}: fire_laser at {step[op]['rate_hz']} Hz: Lumi fires at most "
                             f"{LASER_RATE_MAX_HZ:g} Hz")
        if op == METER_ENERGY:
            mj = step[op].get("mj") if isinstance(step[op], dict) else None
            if not isinstance(mj, (int, float)) or not mj > 0:
                raise ValueError(f"step {i}: meter_energy needs {{\"mj\": > 0}}, got {step[op]!r}")
            step = {**step, METER_ENERGY: {"mj": float(mj)}}
            proto_steps.append({"label": step.get("label", ""), "laser_energy": {"mj": 1.0}})
        else:
            proto_steps.append(step)
        steps[i - 1] = step
    try:
        request = json_format.ParseDict({"steps": proto_steps}, pb.SequenceRequest())
    except json_format.ParseError as e:
        raise ValueError(f"not a pld.proto step list: {e}") from None
    total = _count(request.steps)
    canonical = json.dumps(steps, sort_keys=True, separators=(",", ":"))
    ops = [_op(s) for s in steps]
    return {
        "steps_json": canonical,
        "content_hash": hashlib.sha256(canonical.encode()).hexdigest()[:16],
        "num_steps": len(steps),
        "num_operations": total,
        "fires": FIRE in ops,
        "sets_energy": METER_ENERGY in ops,
    }


def _count(steps) -> int:
    """Operations with the loops multiplied out; checks the loops match."""
    stack: list[tuple[int, int]] = []  # (count, operations so far before the loop)
    total = 0
    for i, step in enumerate(steps, 1):
        which = step.WhichOneof("op")
        if which is None:
            raise ValueError(f"step {i}: no operation")
        if which == "loop_start":
            if not 1 <= step.loop_start <= 100_000:
                raise ValueError(f"step {i}: loop_start is 1..100000, got {step.loop_start}")
            stack.append((step.loop_start, total))
            total = 0
        elif which == "loop_end":
            if not stack:
                raise ValueError(f"step {i}: loop_end without a loop_start")
            count, before = stack.pop()
            total = before + count * total
        else:
            total += 1
        if total > MAX_OPERATIONS:
            raise ValueError(f"more than {MAX_OPERATIONS} operations with the loops multiplied out")
    if stack:
        raise ValueError(f"{len(stack)} loop_start without a loop_end")
    return total


def resolve(steps: list[dict], ratio: float | None) -> list[dict]:
    """The steps pld-service runs: each meter_energy becomes laser_energy at mj / ratio."""
    out = []
    for step in steps:
        if METER_ENERGY in step:
            if ratio is None:
                raise RuntimeError("no laser test today: run the daily test before a sequence that sets the energy")
            source = round(step[METER_ENERGY]["mj"] / ratio, 2)
            out.append({"label": step.get("label") or f"{step[METER_ENERGY]['mj']:g} mJ at the meter",
                        "laser_energy": {"mj": source}})
        else:
            out.append(step)
    return out


# --- what a run grew ---------------------------------------------------------------------

_FIRED = re.compile(r"fired (\d+) of (\d+)")


def _flag(value) -> bool | None:
    return None if value is None else str(value).lower() == "true"


class RunTracker:
    """Follows a run's steps to the layers it grows.

    A layer is the material in the selected slot, for the pulses actually fired while
    the shutter is open; fires one after another on the same material merge (a wedge is
    one layer, an ABAB superlattice stays ABAB). A fire with the shutter in front is a
    pre-ablation, not a layer. The target and shutter come from the step-ended event's
    detail when pld-service sends them ("target", "shutter_away", "pulses_fired"), else
    from the steps themselves and "fired N of M" in the message.
    """

    def __init__(self, steps: list[dict], *, target: str, shutter_away: bool) -> None:
        self.steps = steps
        self.target = target
        self.shutter_away = shutter_away
        self.meter_mj: float | None = None
        self.layer: dict | None = None

    def ended(self, step: int, ok: bool, message: str, detail: dict) -> tuple[dict, dict | None]:
        """One step ended. Returns what it did (for the journal) and the layer it
        closed, if a fire on another material started a new one."""
        spec = self.steps[step - 1] if 1 <= step <= len(self.steps) else {}
        op = detail.get("op") or next((k for k in spec if k != "label"), "")
        if ok and op == "select_target":
            self.target = spec[op].get("target", self.target)
        if ok and op == "sample_shutter":
            self.shutter_away = not spec[op].get("in_front", False)
        if ok and op == METER_ENERGY:
            self.meter_mj = spec[op]["mj"]
        if ok and op == "laser_energy" and isinstance(spec.get("laser_energy"), dict):
            self.meter_mj = None  # a source energy, not one at the meter
        self.target = detail.get("target") or self.target
        shutter = _flag(detail.get("shutter_away"))
        if shutter is not None:
            self.shutter_away = shutter
        info: dict = {"op": op, "target": self.target, "shutter_away": self.shutter_away}
        if op != "fire_laser":
            return info, None
        fired = detail.get("pulses_fired")
        if fired is None:
            m = _FIRED.search(message)
            fired = m.group(1) if m else (spec.get(op, {}).get("pulses") if ok else 0)
        fired = int(fired or 0)
        rate = float(detail.get("rate_hz") or spec.get(op, {}).get("rate_hz") or 0.0)
        info.update(pulses_fired=fired, rate_hz=rate,
                    deposition=self.shutter_away and fired > 0 and self.target != "Clear")
        if not info["deposition"]:
            return info, None
        closed = None
        if self.layer is not None and self.layer["target_id"] != self.target:
            closed, self.layer = self.layer, None
        if self.layer is None:
            self.layer = {"target_id": self.target, "num_pulse": 0, "laser_repetition_rate": rate,
                          "meter_mj": self.meter_mj, "first_step": step, "new": True}
        else:
            self.layer["new"] = False
        self.layer["num_pulse"] += fired
        return info, closed

    def finish(self) -> dict | None:
        """The run ended: the layer still open, if any."""
        layer, self.layer = self.layer, None
        return layer
