"""Chamber handlers: the growth log, PLDconfig.ini, and MI mode."""

from __future__ import annotations

import logging
import queue

from lumi.contracts.payloads.chamber import (
    AllConfigs,
    ChamberConfigState,
    ChamberLogState,
    ConfigEntry,
    ConfigQuery,
    ConfigSection,
    ConfigSections,
    LogBatch,
    LogEntry,
    MICommands,
    MIExecution,
    MIExecutionList,
    MIModeState,
    SectionQuery,
)
from lumi.contracts.payloads.common import Empty

log = logging.getLogger(__name__)


def _entry(row: dict, header: dict) -> LogEntry:
    header = dict(header or {})
    return LogEntry(
        time=float(header.get("time", 0.0)),
        time_stamp=str(header.get("time_stamp", "")),
        values={str(k): v for k, v in (row or {}).items()},
    )


class ChamberLogHandler:
    """The growth log, tailed from disk.

    The old ChamberLogMessageQueueServer ignored `request_type` entirely and always
    returned the log -- so it was a one-op capability pretending to be a dispatcher.
    """

    def __init__(self, log_reader, stream_queue: "queue.Queue | None" = None) -> None:
        self.log_reader = log_reader
        self.stream_queue = stream_queue

    async def log(self, req: Empty) -> LogBatch:
        row, header = self.log_reader.get_log()
        if row is None:
            raise RuntimeError("could not read the chamber log file")
        return LogBatch(entries=[_entry(row, header)])

    async def next(self) -> LogEntry | None:
        if self.stream_queue is None:
            return None
        try:
            row, header = self.stream_queue.get_nowait()
        except queue.Empty:
            return None
        return _entry(row, header)

    def state(self) -> ChamberLogState:
        # Storage sizes its HDF5 log table from these column names, so they have to be
        # in the state rather than inferred from whatever row happens to arrive first.
        columns: list[str] = []
        try:
            row, _ = self.log_reader.get_log()
            if row:
                columns = [str(k) for k in row]
        except Exception:
            log.debug("could not read log columns", exc_info=True)

        return ChamberLogState(
            is_running=self.log_reader.is_alive(),
            log_file=str(getattr(self.log_reader.config, "log_path", "") or ""),
            columns=columns,
        )


class ChamberConfigHandler:
    """PLDconfig.ini, parsed and watched.

    Values arrive already typed -- ConfigReader runs ast.literal_eval on each one, so
    `30000` is an int and `"PLD"` is the string PLD.
    """

    def __init__(self, config_reader) -> None:
        self.config_reader = config_reader

    # ConfigReader (the threaded wrapper, not the parser underneath it) wraps two of
    # its four accessors and not the other two: get_sections -> {"sections": [...]},
    # get_config -> {"config": v}, while get_all_configs and get_configs_by_section
    # return the values raw. The contract hides that.

    async def get_all_config(self, req: Empty) -> AllConfigs:
        return AllConfigs(configs=self.config_reader.get_all_configs())

    async def get_config(self, req: ConfigQuery) -> ConfigEntry:
        try:
            wrapped = self.config_reader.get_config(req.section, req.key)
        except KeyError:
            raise KeyError(f"no config {req.section}.{req.key}") from None
        return ConfigEntry(value=wrapped["config"])

    async def get_configs_by_section(self, req: SectionQuery) -> ConfigSection:
        try:
            values = self.config_reader.get_configs_by_section(req.section)
        except KeyError:
            raise KeyError(f"no config section {req.section!r}") from None
        return ConfigSection(section=req.section, values=values)

    async def get_sections(self, req: Empty) -> ConfigSections:
        return ConfigSections(sections=list(self._sections()))

    def _sections(self) -> list[str]:
        return list(self.config_reader.get_sections()["sections"])

    def state(self) -> ChamberConfigState:
        try:
            sections = self._sections()
        except Exception:
            sections = []
        return ChamberConfigState(
            is_running=self.config_reader.is_alive(),
            config_file=str(getattr(self.config_reader.config, "config_path", "") or ""),
            sections=sections,
        )


class MIModeHandler:
    """Machine-instruction mode: the one genuinely pub/sub capability.

    A client submits a script and gets an acknowledgement immediately; the *result*
    arrives later on the broadcast update channel, correlated by commands_uuid. That
    is why it is PUBSUB and not RPC -- a growth script can run for minutes.
    """

    def __init__(self, mi_server) -> None:
        self.mi_server = mi_server

    async def register_commands(self, req: MICommands) -> MIExecution:
        uuid = req.commands_uuid or ""
        future = self.mi_server.register_commands(commands=req.commands, commands_uuid=uuid)
        # The future resolves when the script finishes; we do not await it. The caller
        # gets the queued execution now, and the completion arrives as an update.
        if future is not None:
            future.add_done_callback(self._on_execution_done)

        execution = self.mi_server.executions.get(uuid)
        if execution is None:
            # A `$stop` / `$clean` special command executes immediately and registers
            # no execution.
            return MIExecution(commands=req.commands, commands_uuid=uuid, state="special")
        return MIExecution(**execution.to_dict())

    async def list_execution(self, req: Empty) -> MIExecutionList:
        return MIExecutionList(
            executions=[MIExecution(**e) for e in self.mi_server.list_executions()]
        )

    async def next_update(self) -> MIExecution | None:
        """Broadcast execution state changes as the MI thread produces them."""
        try:
            content, header = self.mi_server.update_queue.get_nowait()
        except queue.Empty:
            return None

        # The queue carries three kinds of update; only the per-execution ones fit the
        # contract's update model. `all_executions` is a list and is served by the
        # list_execution op instead of being pushed.
        if (header or {}).get("update_content") == "all_executions":
            return None
        if not isinstance(content, dict) or "commands_uuid" not in content:
            return None
        return MIExecution(**content)

    def _on_execution_done(self, future) -> None:
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            log.error("MI execution failed: %s", exc)

    def state(self) -> MIModeState:
        executions = self.mi_server.list_executions()
        running = [e for e in executions if not e.get("is_execution_finished")]
        return MIModeState(
            is_running=self.mi_server.is_alive(),
            num_executions=len(executions),
            current_execution=running[0]["commands_uuid"] if running else None,
        )
