import csv
import numpy as np
import pandas as pd
import ast
import datetime

from lumi.pascal.log_fields import PARSE_DICT, parse_bit_field  # noqa: F401  (re-exported)


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
    # laser_pulses_setpoint = log_df["Laser set"]
    # deposition_window = np.where(laser_pulses>0)[0]
    # deposition_start = deposition_window[0]
    diff_laser_pulses = np.diff(laser_pulses)

    deposition_start = np.where(diff_laser_pulses>0)
    deposition_end = np.where(diff_laser_pulses<0)

    if len(deposition_start[0]) == 0 or len(deposition_end[0]) == 0:
       return None, None
    else:
        deposition_start = deposition_start[0][0]
        deposition_end = deposition_end[0][0]

    if "time_stamp" in log_df.columns:
        log_time = log_df["time_stamp"]
    else:
        log_time = log_df["Time"]

    return log_time[deposition_start], log_time[deposition_end]
