"""Pascal chamber payloads: the growth log, the chamber's INI config, and MI mode."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .common import ServerStateBase

# A parsed PLDconfig.ini value. ConfigReader runs ast.literal_eval on every value,
# so these arrive already typed (30000 -> int, 5.000000 -> float, "PLD" -> str)
# rather than as raw strings.
ConfigValue = int | float | str | bool


class LogEntry(BaseModel):
    """One row of the chamber log."""

    time: float
    time_stamp: str
    values: dict[str, float | str | None]


class LogBatch(BaseModel):
    entries: list[LogEntry] = []


# --- log history ------------------------------------------------------------------

#: What a log read returns when the caller names no columns: the handful a growth is
#: judged by. Asking for all 64 (130 once the status words are split into bits) over a
#: long window is megabytes of JSON, so the full set is opt-in.
DEFAULT_LOG_COLUMNS: tuple[str, ...] = (
    "HT Temp set", "HT Temp moni", "HT moni",
    "Prc Pres Main", "Vac Pres Main", "MFC1 moni", "MFC2 moni",
    "TG No.", "LaserHz", "LaserPuls", "Mask1", "Substrate", "Shut Stat",
)


class LogSeries(BaseModel):
    """A stretch of chamber log as columns rather than rows: `time[i]` goes with
    `columns[name][i]`, which is the shape a chart wants.

    A number where the log holds one, the text otherwise (the status words stay
    `0x0001`, since which bit means what is the reader's business). A column the log
    does not have comes back as all nulls rather than failing the read.
    """

    time: list[float] = []
    columns: dict[str, list[float | str | None]] = {}
    #: Rows in the window before `max_points` thinned them.
    n_rows: int = 0
    #: 1 = every row; k = every k-th row.
    stride: int = 1


class ListLogFiles(BaseModel):
    search: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class LogFileInfo(BaseModel):
    name: str
    size_bytes: int
    modified: float
    n_rows: int
    #: First and last row, epoch seconds. The log's own `Time` column carries no date;
    #: it is taken from the file name (`chamber_log_YYYYMMDD_HHMMSS.csv`, which is how
    #: the driver names them) or failing that from the file's modification time.
    start: float | None = None
    end: float | None = None
    start_iso: str | None = None
    end_iso: str | None = None
    #: The file the chamber node is tailing right now, which is still growing.
    live: bool = False


class LogFileList(BaseModel):
    files: list[LogFileInfo] = []
    total: int = 0


class LogWindowQuery(BaseModel):
    """A time window of the chamber log, across however many files it spans.

    Name a `file` to read that one, or leave it out and give `since`/`until` (epoch
    seconds) to read whatever was logged then -- the growth-history case, where the
    caller knows when an experiment ran but not which file it landed in.
    """

    file: str | None = None
    since: float | None = None
    until: float | None = None
    #: Log column names; None means DEFAULT_LOG_COLUMNS.
    columns: list[str] | None = None
    max_points: int = Field(default=2000, ge=2, le=100_000)


class ConfigQuery(BaseModel):
    section: str
    key: str


class SectionQuery(BaseModel):
    section: str


class ConfigEntry(BaseModel):
    value: ConfigValue


class ConfigSection(BaseModel):
    section: str
    values: dict[str, ConfigValue]


class ConfigSections(BaseModel):
    sections: list[str]


class AllConfigs(BaseModel):
    configs: dict[str, dict[str, ConfigValue]]


# --- MI mode ---------------------------------------------------------------
# MI mode is the one genuinely pub/sub capability: a client registers a command
# script, gets an immediate acknowledgement, and the *result* arrives later on the
# broadcast update channel, correlated by commands_uuid.


class MICommands(BaseModel):
    commands: str
    commands_uuid: str | None = None


class MIExecution(BaseModel):
    """One submitted command script and where it has got to.

    Mirrors MIExecution.to_dict() in lumi.pascal.mi_mode -- which is the shape that
    has always been on the wire, just never written down anywhere a client could see.
    """

    commands: str
    commands_uuid: str
    state: str
    is_execution_finished: bool = False
    is_aborted: bool = False
    is_cleaned_up: bool = False
    is_stopped: bool = False


class MIExecutionList(BaseModel):
    executions: list[MIExecution] = []


class ChamberLogReadout(BaseModel):
    log_file: str | None = None
    n_entries: int | None = None
    last_entry_time: str | None = None
    # The log's column names. Storage needs these to size its HDF5 dataset -- without
    # them it creates a (N, 0) table and silently records nothing.
    columns: list[str] = []


class ChamberLogState(ChamberLogReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the log reader's readout.
    pass


class ChamberConfigReadout(BaseModel):
    config_file: str | None = None
    sections: list[str] = []


class ChamberConfigState(ChamberConfigReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the config reader's readout.
    pass


class MIModeReadout(BaseModel):
    num_executions: int = 0
    current_execution: str | None = None


class MIModeState(MIModeReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the MI server's readout.
    pass
