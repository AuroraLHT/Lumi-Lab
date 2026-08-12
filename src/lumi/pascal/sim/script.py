"""Parse an MI script back into instructions the model can execute.

`lumi.pascal.command` is a one-way encoder: every `PascalCommand.to_text()` renders a
line for the controller and nothing reads it back. The simulator needs the other
direction, so this is the matching decoder -- the patterns below are written directly
against those `to_text()` bodies and `tests/pascal/test_chamber_sim.py` round-trips
every command the experiment manager emits through both halves.

Anything unrecognised is kept as a `noop` instruction rather than failing the script:
the real controller accepts a much larger command set than `lumi.pascal.command`
encodes, and aborting a growth because the simulator has not learned a line yet would
be a worse lie than quietly not modelling it. Unrecognised lines are logged once each.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# How many instructions a single script may expand to once loops are unrolled. A
# `Loop 1000000` typo should fail the script, not wedge the simulator thread.
MAX_INSTRUCTIONS = 200_000


class ScriptError(Exception):
    """The script cannot be executed -- malformed loop nesting, or too large."""


@dataclass
class Instruction:
    kind: str
    args: dict = field(default_factory=dict)
    text: str = ""
    sync: bool = False
    nowait: bool = False


_NUM = r"[-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?"

# (pattern, kind, argument names). Order matters: the longer "Heating Laser Lock" and
# "Set Mask Position" forms have to be tried before the "Heating Laser" and
# "Move Mask" prefixes they contain.
_PATTERNS: list[tuple[re.Pattern, str, tuple[str, ...]]] = [
    (re.compile(r"^Loop\s+(\d+)\s*\(\d+\)$"), "loop_start", ("iteration",)),
    (re.compile(r"^Loop End$"), "loop_end", ()),
    (re.compile(rf"^Wait\s+({_NUM})\s*sec\s*\(\d+\)$"), "wait", ("seconds",)),
    (re.compile(r"^Wait for Continue$"), "wait_for_continue", ()),
    (re.compile(r"^Beep$"), "beep", ()),

    (re.compile(r"^Select Target\s+(\S+)$"), "select_target", ("target",)),
    (re.compile(r"^Target Rotation Mode\s+(\S+)$"), "target_rotation_mode", ("mode",)),
    (re.compile(r"^Target Twist Mode\s+(\S+)$"), "target_twist_mode", ("mode",)),
    (re.compile(r"^Seek Targets Home$"), "seek_targets_home", ()),

    (re.compile(rf"^Set Mask Position M(\d)=({_NUM})$"), "set_mask_position", ("mask_id", "distance")),
    (re.compile(rf"^Move Mask M(\d)=({_NUM})$"), "move_mask", ("mask_id", "distance")),
    (re.compile(r"^Seek Mask Home M(\d)$"), "seek_mask_home", ("mask_id",)),
    (re.compile(r"^Mask Speed M(\d) Low=(\d+) High=(\d+) Accel=(\d+)$"), "mask_speed",
     ("mask_id", "low", "high", "accel")),

    (re.compile(rf"^Set Sample Position\s+({_NUM})$"), "set_sample_position", ("position",)),
    (re.compile(rf"^Rotate Sample\s+({_NUM})$"), "rotate_sample", ("angle",)),
    (re.compile(r"^Sample Rotation Mode\s+(\S+)$"), "sample_rotation_mode", ("mode",)),
    (re.compile(r"^Sample Shutter\s+(ON|OFF)$"), "sample_shutter", ("state",)),

    (re.compile(r"^Heating Laser Pointer\s+(ON|OFF)$"), "heating_laser_pointer", ("state",)),
    (re.compile(r"^Heating Laser Lock\s+(LOCKED|ACTIVE)$"), "heating_laser_lock", ("state",)),
    (re.compile(r"^Heating Laser Threshold\s+(ON|OFF)$"), "heating_laser_threshold", ("state",)),
    (re.compile(r"^Heating Laser\s+(ON|OFF)$"), "heating_laser", ("state",)),
    (re.compile(rf"^Set Heating Current\s+({_NUM})$"), "set_heating_current", ("current",)),
    (re.compile(rf"^Set Minimum Current\s+({_NUM})$"), "set_minimum_current", ("current",)),
    (re.compile(rf"^Set Maximum Current\s+({_NUM})$"), "set_maximum_current", ("current",)),

    (re.compile(rf"^Temperature Tolerance\s+({_NUM})$"), "temperature_tolerance", ("tolerance",)),
    (re.compile(r"^Temperature Ramp\s+(OFF|ON)$"), "temperature_ramp_state", ("state",)),
    (re.compile(rf"^Temperature Ramp\s+({_NUM})$"), "temperature_ramp", ("rate",)),
    (re.compile(rf"^Temperature Set\s+({_NUM})$"), "temperature_set", ("temperature",)),
    (re.compile(r"^Temperature Control\s+(Manual|PID)$"), "temperature_control", ("mode",)),

    (re.compile(r"^Depo Laser Gate\s+(ON|OFF)$"), "depo_laser_gate", ("state",)),
    (re.compile(rf"^Trigger Laser N=(\d+)\s*\(\d+\)\s*F=({_NUM})$"), "trigger_laser",
     ("num_pulse", "frequency")),
    (re.compile(rf"^Combi N=(\d+)\s*\(\d+\)\s*F=({_NUM})\s*M1=({_NUM})\s*M2=({_NUM})$"), "combi",
     ("num_pulse", "frequency", "mask1", "mask2")),

    (re.compile(r"^MFC Control\s+(Enable|Disable)$"), "mfc_control", ("state",)),
    (re.compile(rf"^MFC(\d) Flow Set=\s*({_NUM})$"), "mfc_flow", ("mfc_id", "flow")),
    (re.compile(r"^Valve\s+(ON|OFF)\s+(\S+)$"), "valve", ("state", "valve")),
    (re.compile(r"^Press Control\s+(\S+)$"), "select_control_mfc", ("mfc",)),
    (re.compile(r"^Pressure Gauge\s+(\S+)$"), "select_pressure_gauge", ("gauge",)),
    (re.compile(rf"^Set Pressure=\s*({_NUM})$"), "set_pressure", ("pressure",)),
    (re.compile(r"^Pressure Control\s+(ON|OFF)$"), "pressure_control", ("state",)),

    (re.compile(r"^Select Coil Position\s+(\S+)$"), "select_coil_position", ("position",)),
    (re.compile(rf"^Set RHEED Gun-X axis\s*=\s*({_NUM})$"), "set_rheed_gun_x", ("position",)),
    (re.compile(r"^Log Interval\s+(\d+)$"), "log_interval", ("seconds",)),
    (re.compile(r"^Data Logging File=(.*)$"), "data_logging_on", ("file_name",)),
    (re.compile(r"^Data Logging OFF$"), "data_logging_off", ()),
]

_NUMERIC_ARGS = {
    "iteration", "seconds", "distance", "position", "angle", "current", "tolerance",
    "rate", "temperature", "num_pulse", "frequency", "mask1", "mask2", "flow",
    "pressure", "mask_id", "low", "high", "accel",
}

_INT_ARGS = {"iteration", "num_pulse", "mask_id", "low", "high", "accel", "seconds"}


def parse_line(line: str) -> Instruction | None:
    """One script line -> one instruction. Returns None for blanks and comments."""
    text = line.strip()
    # PascalScope indents nested blocks with "| " per level.
    while text.startswith("|"):
        text = text[1:].lstrip()
    if not text or text.startswith("/"):
        return None

    sync = nowait = False
    # The mixins append the flags in a fixed order (sync then nowait), but strip them
    # in a loop so either alone, or a hand-written script's reversed pair, still parses.
    changed = True
    while changed:
        changed = False
        for suffix, flag in ((" (Nowait)", "nowait"), (" (Sync)", "sync")):
            if text.endswith(suffix):
                text = text[: -len(suffix)].rstrip()
                if flag == "nowait":
                    nowait = True
                else:
                    sync = True
                changed = True

    for pattern, kind, names in _PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        args: dict = {}
        for name, raw in zip(names, match.groups()):
            if name in _NUMERIC_ARGS:
                args[name] = int(float(raw)) if name in _INT_ARGS else float(raw)
            else:
                args[name] = raw
        return Instruction(kind=kind, args=args, text=text, sync=sync, nowait=nowait)

    return Instruction(kind="noop", args={}, text=text, sync=sync, nowait=nowait)


def parse_script(source: str) -> list[Instruction]:
    """Parse and unroll a whole script into a flat instruction list.

    Loops are expanded here rather than interpreted at run time: it keeps the executor
    a straight `for` over instructions, and it means a runaway `Loop` is rejected at
    submit time instead of discovered thirty minutes into a growth.
    """
    instructions = [i for i in (parse_line(line) for line in source.splitlines()) if i is not None]
    unrolled, index = _unroll(instructions, 0, len(instructions))
    if index != len(instructions):
        raise ScriptError("unbalanced 'Loop End' -- more ends than starts")
    return unrolled


def _unroll(instructions: list[Instruction], start: int, stop: int) -> tuple[list[Instruction], int]:
    out: list[Instruction] = []
    i = start
    while i < stop:
        instruction = instructions[i]
        if instruction.kind == "loop_end":
            return out, i
        if instruction.kind == "loop_start":
            body, end = _unroll(instructions, i + 1, stop)
            if end >= stop or instructions[end].kind != "loop_end":
                raise ScriptError(f"'Loop {instruction.args['iteration']}' has no 'Loop End'")
            iterations = int(instruction.args["iteration"])
            if iterations * max(len(body), 1) + len(out) > MAX_INSTRUCTIONS:
                raise ScriptError(
                    f"script expands to more than {MAX_INSTRUCTIONS} instructions "
                    f"('Loop {iterations}' over {len(body)} lines)"
                )
            for _ in range(iterations):
                out.extend(body)
            i = end + 1
            continue
        out.append(instruction)
        if len(out) > MAX_INSTRUCTIONS:
            raise ScriptError(f"script is longer than {MAX_INSTRUCTIONS} instructions")
        i += 1
    return out, i
