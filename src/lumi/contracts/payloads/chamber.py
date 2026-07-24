"""Pascal chamber payloads: the growth log, the chamber's INI config, and MI mode."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

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
