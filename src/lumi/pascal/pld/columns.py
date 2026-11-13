"""pld-service's state in PLD Controller's log columns.

Shared by the chamber node's growth-log writer (`monitor.GrowthLog`) and the experiment
node's readings (`lumi.experiment.chamber.PldChamber`), so a value reads the same off
the CSV and off the live state. No grpc here: the experiment node does not install it.
"""

from __future__ import annotations

import math

from lumi.pascal.sim.columns import LOG_COLUMNS

#: PLDconfig.ini TG1..TG7.
TARGET_NUMBERS = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "Clear": 7}
#: `Valve Stat1` and `Valve Stat2` bits, as `lumi.pascal.log_fields.PARSE_DICT` decodes
#: them (1 = open). PLD Controller's MV2/FV2 (RHEED) are pld-service's MV20/FV20.
#: Confirmed by REA (2026-11-10) against PLD Controller's own log, each named PLC
#: capture flipping exactly one bit. The idle 0x077F is everything open but RV3, VT1, VT2.
VALVE_STAT1 = {"MV10": 0, "MV11": 1, "FV1": 2, "MV20": 3, "FV20": 4, "MV3": 5, "FV3": 6, "RV3": 7,
               "MV21": 8, "FV21": 9, "CV1": 10, "VT1": 11, "VT2": 12}
VALVE_STAT2 = {"CV301": 2}
#: `Pump Stat` bits. PLD Controller's TMP2 (RHEED) and TMP4 (2nd RHEED) are TMP20 and
#: TMP21: DP1 backs TMP1 and TMP20, DP2 backs TMP21 alone, as the service's pump
#: interlocks say. The names are PLD Controller's own buttons (relays 06004/06006):
#: TMP20 is the RHEED line's first pump, far from the electron gun, and TMP21 its
#: second, beside the gun (the lab, 2026-11-10).
PUMP_STAT = {"DP1": 0, "DP2": 1, "DP3": 2, "TMP1": 3, "TMP20": 4, "TMP3": 5, "TMP21": 6}



def _num(value) -> float:
    """NaN (the proto's "no reading") and None (the payload's) are written as 0."""
    if value is None:
        return 0.0
    value = float(value)
    return value if math.isfinite(value) else 0.0


def _word(flags: dict[int, bool]) -> int:
    return sum(1 << bit for bit, on in flags.items() if on)


def values_from_state(s) -> dict:
    """A `pld.v1.ChamberState` -- or its `PldChamberState` payload, which mirrors its
    field names -- as one log row's values, keyed by LOG_COLUMNS."""
    h, p, m, laser, g = s.heater, s.pressures, s.motion, s.laser, s.gas
    temperature = _num(h.temperature)
    temp_set = _num(h.temperature_setpoint) if h.pid_on else 0.0
    values = {c: 0 for c in LOG_COLUMNS[1:]}
    values.update({
        "TG No.": TARGET_NUMBERS.get(m.target, 0),
        "TG Rotate": _num(m.target_angle),
        "Mask1": _num(m.mask_position),
        "Mask2": _num(m.rheed_gun_x),
        "Substrate": _num(m.sample_angle),
        "LaserHz": laser.rate_hz if laser.status == "firing" else 0.0,
        "LaserPuls": laser.pulses_fired,
        # As the simulator writes them: the train's count so far, and what it was asked for.
        "Laser moni": laser.pulses_fired,
        "Laser set": laser.pulses_requested,
        "MissedPuls": laser.missed_shots,
        "HT set": _num(h.current_set),
        "HT moni": _num(h.current_monitor),
        "Delta HT curr": _num(h.current_set) - _num(h.current_monitor),
        "HT Temp set": temp_set,
        "HT Temp moni": temperature,
        "Delta Temp": (temp_set - temperature) if h.pid_on else 0.0,
        "MFC1 set": _num(g.mfc1_setpoint),
        "MFC1 moni": _num(g.mfc1_flow),
        "Delta MFC1": _num(g.mfc1_setpoint) - _num(g.mfc1_flow),
        "Prc Pres Main": _num(p.cdg10),
        "Prc Pres Main2": _num(p.cdg11),
        "Vac Pres Main": _num(p.ig1),
        "Back Pres Main": _num(p.tcg[0]) if len(p.tcg) else 0.0,
        "Vac Pres L/L": _num(p.ig3),
        "Back Pres L/L": _num(p.tcg31),
        "Heat Stat": _word({0: h.power_on, 1: h.pid_on, 2: h.ramp_c_per_min > 0, 3: h.started}),
        "Valve Stat1": _word({bit: s.valves.get(name, False) for name, bit in VALVE_STAT1.items()}),
        "Valve Stat2": _word({bit: s.valves.get(name, False) for name, bit in VALVE_STAT2.items()}),
        "Pump Stat": _word({bit: pump.running for name, bit in PUMP_STAT.items()
                            if (pump := s.pumps.get(name)) is not None}),
        "Shut Stat": _word({0: s.sample_shutter_away}),
        "Motor Stat": _word({0: m.motor_free, 1: m.target_spinning}),
    })
    return values
