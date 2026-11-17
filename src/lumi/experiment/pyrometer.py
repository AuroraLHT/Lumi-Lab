"""Which emissivity the chamber's back pyrometer ran at, for data that did not record it.

The back pyrometer is the only temperature the driver reads, sets and records. It looks
at the backside of the SiC heater block, never at the substrate, through a viewport, so
its emissivity is an effective one (the block plus the viewport's absorption) and has
to be calibrated. It was recalibrated the week of 2026-10-19, from 0.65 to 0.262, and
the old setting reads far low: 650 C then was about 794 C now. A temperature therefore
means nothing without the emissivity it was read at.

From now on every growth record and recording stores the emissivity the pyrometer
reported (pld-service's heater.emissivity). Data from before that has none, and gets it
from this history: when growth.db gains the column, and when a v1 recording is
converted to v2.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

#: Before the recalibration.
OLD_EMISSIVITY = 0.65
#: From the recalibration on (week of 2026-10-19).
NEW_EMISSIVITY = 0.262

#: The first growth on the new setting: experiment 23 (substrate 26, HZO-YSZ-04).
FIRST_NEW_EXPERIMENT_ID = 23
#: Recordings that started before this, lab time, are on the old setting.
FIRST_NEW_DAY = dt.datetime(2026, 10, 22, tzinfo=ZoneInfo("America/New_York"))


def emissivity_for_experiment(experiment_id: int) -> float:
    return OLD_EMISSIVITY if experiment_id < FIRST_NEW_EXPERIMENT_ID else NEW_EMISSIVITY


def emissivity_at(epoch: float) -> float:
    return OLD_EMISSIVITY if epoch < FIRST_NEW_DAY.timestamp() else NEW_EMISSIVITY
