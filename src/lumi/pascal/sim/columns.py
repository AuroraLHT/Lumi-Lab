"""The chamber log's column schema and PASCAL's exact text formatting.

The simulator writes a CSV that the production `lumi.pascal.log_reader.LogReader`
tails and `lumi.pascal.chamber_log.process_row` parses, so the text has to match what
the real controller writes -- not merely be numerically equal:

- `process_row` runs `ast.literal_eval` on every status word, so those must stay
  `0x....` and not become decimal.
- `process_row` parses `Time` with `datetime.strptime(row["Time"], "%I:%M:%S %p")`,
  which is 12-hour with no leading zero on the hour and an AM/PM suffix.
- `ExperimentManager.get_current_pressure` compares a pressure as a *string*:
  `str(values["Vac Pres Main"]) == "0.00E+0"`. Emitting `0.0` or `0.00E+00` there
  silently takes the wrong branch.

The formats below were read column-by-column off `assets/chamber_log_test.csv`, which
is a raw controller dump. `chamber_log_test_v2.csv` is the same kind of data re-saved
through pandas -- it has lost the fixed decimals (`51.5` where the controller writes
`51.50`) and so is not the format reference.
"""

from __future__ import annotations

import datetime

# The 64 columns, in the controller's order. Storage sizes its HDF5 log table from
# whatever column list the chamber advertises, so the order is part of the contract
# with the recorder, not just cosmetic. `tests/pascal/test_chamber_sim.py` asserts this
# still equals the header of the recorded asset.
LOG_COLUMNS: tuple[str, ...] = (
    "Time",
    "TG No.", "TG Rotate", "TG Spin Speed", "TG-Z",
    "Mask1", "Mask2", "Substrate",
    "FocusLns", "MirrorPs", "ATN", "BTFvalve",
    "LaserHz", "LaserPuls", "Laser moni", "Laser set", "MissedPuls", "PwrMeter",
    "HT set", "HT moni", "Delta HT curr",
    "HT Temp set", "HT Temp moni", "Delta Temp",
    "MFC1 set", "MFC1 moni", "Delta MFC1",
    "MFC2 set", "MFC2 moni", "Delta MFC2",
    "MFC3 set", "MFC3 moni", "Delta MFC3",
    "MFC4 set", "MFC4 moni", "Delta MFC4",
    "MFC5 set", "MFC5 moni", "Delta MFC5",
    "Prc Pres Main", "Prc Pres Main2", "Vac Pres Main", "Back Pres Main",
    "Prc Pres L/L", "Vac Pres L/L", "Back Pres L/L",
    "Heat Stat", "DepoLaserStat", "Pump Stat",
    "Valve Stat1", "Valve Stat2", "Shut Stat", "Motor Stat", "Other Stat",
    "A Utility1", "A Utility2", "A Pump1", "A Pump2",
    "A EXT1", "A EXT2", "A Motor1", "A Motor2",
    "W Pross", "W etc",
)

# Column -> how the controller prints it. "e2"/"e3" are scientific with that many
# mantissa decimals; "hex" is a 16-bit status word.
_FORMATS: dict[str, str] = {
    "TG No.": "int", "TG Rotate": "f2", "TG Spin Speed": "int", "TG-Z": "f2",
    "Mask1": "f2", "Mask2": "f2", "Substrate": "f2",
    "FocusLns": "f2", "MirrorPs": "f2", "ATN": "f2", "BTFvalve": "f2",
    "LaserHz": "int", "LaserPuls": "int", "Laser moni": "int", "Laser set": "int",
    "MissedPuls": "int", "PwrMeter": "int",
    "HT set": "f2", "HT moni": "f2", "Delta HT curr": "f2",
    # The heater's temperature triple is integer-valued in the controller's log even
    # though the setpoint command (`Temperature Set 750.0`) takes one decimal.
    "HT Temp set": "int", "HT Temp moni": "int", "Delta Temp": "int",
    "Prc Pres Main": "e2", "Prc Pres Main2": "e3", "Vac Pres Main": "e2",
    "Back Pres Main": "e2", "Prc Pres L/L": "f2", "Vac Pres L/L": "e2",
    "Back Pres L/L": "e2",
}
for _i in (1, 2, 3, 4, 5):
    _FORMATS[f"MFC{_i} set"] = "f2"
    _FORMATS[f"MFC{_i} moni"] = "f2"
    _FORMATS[f"Delta MFC{_i}"] = "f2"
for _c in (
    "Heat Stat", "DepoLaserStat", "Pump Stat", "Valve Stat1", "Valve Stat2",
    "Shut Stat", "Motor Stat", "Other Stat", "A Utility1", "A Utility2",
    "A Pump1", "A Pump2", "A EXT1", "A EXT2", "A Motor1", "A Motor2",
    "W Pross", "W etc",
):
    _FORMATS[_c] = "hex"


def format_scientific(value: float, decimals: int = 2) -> str:
    """`1.11E-3`, `1.040E+0`, `0.00E+0` -- the controller's exponent form.

    Python writes a two-digit, zero-padded exponent (`1.11E-03`); PASCAL writes the
    minimum digits but *keeps* the sign, including on positives. Note this differs
    from `lumi.pascal.command._drop_tailling_zero`, which drops the `+` -- that one
    formats a pressure into a *command* (`Set Pressure= 1.00E-2`), not into the log.
    """
    mantissa, exponent = f"{value:.{decimals}E}".split("E")
    sign, digits = exponent[0], exponent[1:]
    return f"{mantissa}E{sign}{int(digits):d}"


def format_time(when: datetime.datetime) -> str:
    """`2:51:59 PM` -- 12-hour, no leading zero on the hour.

    `%-I` is a glibc extension, so build it by hand rather than assume the platform.
    """
    hour = when.hour % 12 or 12
    return f"{hour}:{when:%M:%S} {when:%p}"


def format_value(column: str, value) -> str:
    kind = _FORMATS.get(column)
    if kind == "int":
        return f"{int(round(float(value))):d}"
    if kind == "f2":
        return f"{float(value):.2f}"
    if kind == "e2":
        return format_scientific(float(value), 2)
    if kind == "e3":
        return format_scientific(float(value), 3)
    if kind == "hex":
        return f"0x{int(value) & 0xFFFF:04X}"
    return str(value)


def render_row(values: dict, when: datetime.datetime) -> list[str]:
    """One CSV row, in `LOG_COLUMNS` order, formatted the way PASCAL formats it."""
    row = [format_time(when)]
    for column in LOG_COLUMNS[1:]:
        row.append(format_value(column, values[column]))
    return row


def bits(**flags: bool) -> int:
    """`bits(**{"0": True, "2": True}) -> 0x0005`. Kept for symmetry with the
    `PARSE_DICT` bit maps in `lumi.pascal.chamber_log`, which are keyed by bit index."""
    word = 0
    for index, on in flags.items():
        if on:
            word |= 1 << int(index)
    return word
