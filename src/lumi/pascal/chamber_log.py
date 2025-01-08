import csv
import numpy as np
import pandas as pd
import ast
import datetime

PARSE_DICT = {}
PARSE_DICT["Heat Stat"] = {
    0: "Heater power supply (PS)",
    1: "PID Heater control",
    2: "Ramp rate setting",
    3: "ON/OFF monitor in PS",
}

PARSE_DICT["DepoLaserStat"] = {
    0: "Inter-locking in excimer laser",
    1: "HV in excimer laser",
    2: "Gate shutter",
}

PARSE_DICT["Pump Stat"] = {
    0: "DP1 (Main)",
    1: "DP2 (2nd RHEED)",
    2: "DP3 (L/L)",
    3: "TMP1 (Main)",
    4: "TMP2 (RHEED)",
    5: "TMP3 (L/L)",
    6: "TMP4 (2nd RHEED)",
}

PARSE_DICT["Valve Stat1"] = {
    0: "MV10 (Main)",
    1: "MV11 (Main bypass)",
    2: "FV1 (Main)",
    3: "MV2 (RHEED)",
    4: "FV2 (RHEED)",
    5: "MV3 (L/L)",
    6: "FV3 (L/L)",
    7: "RV3 (L/L)",
}


PARSE_DICT["Shut Stat"] = {0: "Sample Shutter"}


PARSE_DICT["Motor Stat"] = {
    0: "Target spin",
    1: "Motor free",
}

PARSE_DICT["Other Stat"] = {
    0: "Baking",
}

PARSE_DICT["A Utility1"] = {
    0: "Emergency shutdown",
    1: "Compress air",
}

PARSE_DICT["A Pump1"] = {
    0: "TMP1 (Main) alarm",
    1: "TMP20 (RHEED) alarm",
    2: "TMP3 (L/L) alarm",
    3: "TMP21 (2nd RHEED) alarm",
    8: "DP1 (Main) Thermal",
    9: "DP2 (2nd RHEED) Thermal",
    10: "DP3 (L/L) Thermal",
    11: "DP1 (Main) Vac Error",
    12: "DP2 (2nd RHEED) Vac Error",
    13: "DP3 (L/L) Vac Error",
}

PARSE_DICT["A Pump2"] = {
    8: "Break in baking temp sensor",
    9: "Tripped in baking heater line",
}

PARSE_DICT["A EXT1"] = {
    1: "Door open in laser shield",
    8: "Heater power supply alarm",
    9: "Chiller (for heater) alarm",
    10: "Temperature sensor failure",
    11: "Laser fiber head failure",
    12: "Pyrometer error",
}

PARSE_DICT["A Motor1"] = {
    0: "Pulse failure in sample rotation",
    4: "Pulse failure in TG revolution",
    5: "Pulse failure in TG height (Z)",
    8: "Pulse failure in Mask 1",
    9: "Step out in L-mask encoder",
    10: "Limit sensor in Mask 1",
    11: "Pulse failure in Mask 2",
    12: "Limit sensor in Mask 2",
}

PARSE_DICT["A Motor2"] = {
    0: "Pulse failure in focus lens (R)",
    1: "Driver trouble in focus lens (R)",
    2: "Pulse failure in mirror (R)",
    3: "Driver trouble in mirror (R)",
    4: "Pulse failure in attenuator (R)",
    5: "Limit sensor in attenuator (R)",
}

PARSE_DICT["W Pross"] = {
    0: "Miss-shot in excimer laser",
    1: "Large temperature deviation",
    2: "Large drive-current deviation",
}

PARSE_DICT["W etc"] = {
    0: "T",
    1: "Mask1 confliction",
}


def parse_bit_field(bit_field, bit_headers):
    num = ast.literal_eval(bit_field)
    output = {}
    for bit_loc, bit_header in bit_headers.items():
        bit_mask = 1 << bit_loc
        value = (num & bit_mask) > 0
        output[bit_header] = value
    return output


def show_in_bit(bit_field, bit_length=16):
    num = ast.literal_eval(bit_field)

    formatter = f"{{:0>{bit_length}b}}"
    bit_repr = formatter.format(num)

    return bit_repr


def process_row(row):
    today = datetime.date.today()
    time_info = datetime.datetime.strptime(row["Time"], "%I:%M:%S %p").time()
    time_info = datetime.datetime.combine(today, time_info)
    # row['Time'] = f"{today.isoformat()} {row['Time']}"
    row["Time"] = time_info.isoformat()
    row["time_stamp"] = time_info.isoformat()
    row["time"] = time_info.timestamp()

    for k, v in PARSE_DICT.items():
        row.update(parse_bit_field(row[k], v))
    return row


TYPE_CONVERSIONS = {
    "Time": pd.to_datetime,
    "time_stamp": pd.to_datetime,
    "time": float,

    # Float columns (measurements and settings)
    "TG Rotate": float,
    "TG Spin Speed": float,
    "TG-Z": float,
    "Substrate": float,
    "FocusLns": float,
    "MirrorPs": float,
    "ATN": float,
    "BTFvalve": float,
    "LaserHz": float,
    "Laser moni": int,
    "Laser set": int,
    "PwrMeter": float,
    "HT set": float,
    "HT moni": float,
    "Delta HT curr": float,
    "HT Temp set": float,
    "HT Temp moni": float,
    "Delta Temp": float,
    # MFC related floats
    "MFC1 set": float,
    "MFC1 moni": float,
    "Delta MFC1": float,
    "MFC2 set": float,
    "MFC2 moni": float,
    "Delta MFC2": float,
    "MFC3 set": float,
    "MFC3 moni": float,
    "Delta MFC3": float,
    "MFC4 set": float,
    "MFC4 moni": float,
    "Delta MFC4": float,
    "MFC5 set": float,
    "MFC5 moni": float,
    "Delta MFC5": float,
    # Pressure readings (scientific notation)
    "Prc Pres Main": float,
    "Prc Pres Main2": float,
    "Vac Pres Main": float,
    "Back Pres Main": float,
    "Prc Pres L/L": float,
    "Vac Pres L/L": float,
    "Back Pres L/L": float,

    "Mask1": float,
    "Mask2": float,
    # Integer columns
    "TG No.": int,
    "LaserPuls": int,
    "MissedPuls": int,
    # Hex status columns (keep as strings)
    "Heat Stat": str,
    "DepoLaserStat": str,
    "Pump Stat": str,
    "Valve Stat1": str,
    "Valve Stat2": str,
    "Shut Stat": str,
    "Motor Stat": str,
    "Other Stat": str,
    "A Utility1": str,
    "A Utility2": str,
    "A Pump1": str,
    "A Pump2": str,
    "A EXT1": str,
    "A EXT2": str,
    "A Motor1": str,
    "A Motor2": str,
    "W Pross": str,
    "W etc": str,
}

for k, v in PARSE_DICT.items():
    for _, sub_v in v.items():
        TYPE_CONVERSIONS[sub_v] = bool


def find_deposition_window(log_df):
    """
        return start and end point of the deposition based on the log dataset record
    """
    laser_pulses = log_df["Laser moni"]
    laser_pulses_setpoint = log_df["Laser set"]
    deposition_start = np.where(laser_pulses>0)[0]
    deposition_end = np.where(laser_pulses==laser_pulses_setpoint)[0]
    log_time = log_df["time_stamp"]

    return log_time[deposition_start], log_time[deposition_end]
