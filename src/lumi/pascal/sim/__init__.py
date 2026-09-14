"""A semi-realistic PASCAL chamber simulator.

`--src test` replays a recorded CSV, which is fine for "is the transport alive?" and
useless for anything else: the recorded session is an *idle* chamber. In
`assets/chamber_log_test.csv` the laser never fires, `HT Temp moni` is a constant 160,
and the target never changes, so nothing that drives the chamber can be exercised
against it -- and the MI backend simulator never even reads the script it claims to be
running. (`Motor Stat` is `0x0000` for all 5944 rows, which is simply a healthy idle
chamber: bit 0 "Motor free" is the holding lock *released*, and a powered chamber
holds it clear.)

`--src sim` replaces the recording with a state model. MI commands mutate the model,
the model integrates forward in time, and the chamber log is *rendered from the model*
-- so `Trigger Laser N=3000 F=10` really does take 300 s, advance `LaserPuls` by 3000,
open the gate bit for exactly that long, and leave the log showing a deposition window
that `chamber_log.find_deposition_window` can find.

Three pieces:

- `columns` -- the 64-column schema and PASCAL's exact text formatting.
- `model`   -- the chamber itself: setpoints in, physics forward, a log row out.
- `script`  -- parses the MI script text back into calls on the model.
- `runner`  -- the two threads: one writes the CSV, one executes MI scripts.
"""

from lumi.pascal.sim.columns import LOG_COLUMNS, render_row
from lumi.pascal.sim.model import ChamberModel, ChamberSimConfig
from lumi.pascal.sim.runner import SimLogWriter, SimMIBackend, build_chamber_sim
from lumi.pascal.sim.script import ScriptError, parse_script

__all__ = [
    "LOG_COLUMNS",
    "ChamberModel",
    "ChamberSimConfig",
    "ScriptError",
    "SimLogWriter",
    "SimMIBackend",
    "build_chamber_sim",
    "parse_script",
    "render_row",
]
