"""Chamber handlers: the growth log, PLDconfig.ini, MI mode, and fiducial markers."""

from __future__ import annotations

import logging
import queue

from lumi.contracts.payloads.chamber import (
    AllConfigs,
    ChamberConfigReadout,
    ChamberLogReadout,
    ConfigEntry,
    ConfigQuery,
    ConfigSection,
    ConfigSections,
    LogBatch,
    LogEntry,
    MICommands,
    MIExecution,
    MIExecutionList,
    MIModeReadout,
    SectionQuery,
)
from lumi.contracts.payloads.common import Ack, Empty
from lumi.contracts.payloads.fiducial import (
    KNOWN_ROLES,
    FiducialMarker,
    FiducialReadout,
    MarkerHistory,
    MarkerHistoryQuery,
    MarkerId,
    MarkerList,
    MarkerPoint,
    MarkerStatsSample,
    RoleAssignment,
    RoleMap,
    RoleQuery,
)

log = logging.getLogger(__name__)


def _entry(row: dict, header: dict) -> LogEntry:
    header = dict(header or {})
    row = row or {}

    # Every LogReader (path / sim / test) hands the handler `(row, {})` -- the
    # second element is a `LogContentHeader`, an empty placeholder TypedDict that
    # nothing ever fills. The parsed clock lives in the row instead (`process_row`
    # writes `time`, `time_stamp` and `Time` there). Reading only the header left
    # every wire LogEntry at time=0.0 / time_stamp="" on a live chamber -- so
    # get_current_log, the `log` stream and check_logging_alive all saw a frozen
    # clock. Read the header first in case a future producer starts filling it,
    # then fall back to the row.
    def _pick(*keys):
        for src in (header, row):
            for k in keys:
                v = src.get(k)
                if v not in (None, ""):
                    return v
        return None

    raw_time = _pick("time")
    raw_stamp = _pick("time_stamp", "Time")
    return LogEntry(
        time=float(raw_time) if raw_time not in (None, "") else 0.0,
        time_stamp=str(raw_stamp) if raw_stamp is not None else "",
        values={str(k): v for k, v in row.items()},
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
        # A dead reader thread is worse than no reader: get_log() goes on returning the
        # last row it managed to read, so every consumer sees a plausible but frozen
        # chamber -- `Motor free` stuck, the temperature stuck -- and drives on. Fail
        # loudly instead, since a caller can retry or give up but cannot detect this.
        if not self.log_reader.is_alive():
            raise RuntimeError(
                "the chamber log reader thread is not running; the last row it read is "
                "stale and must not be trusted. Restart the chamber node."
            )
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

    def readout(self) -> ChamberLogReadout:
        # Storage sizes its HDF5 log table from these column names, so they have to be
        # in the state rather than inferred from whatever row happens to arrive first.
        columns: list[str] = []
        try:
            row, _ = self.log_reader.get_log()
            if row:
                columns = [str(k) for k in row]
        except Exception:
            log.debug("could not read log columns", exc_info=True)

        # Deliberately not reporting reader_alive/skipped_rows here: both would be
        # useful on the monitor, but ChamberLogReadout is part of the contract and new
        # fields bump the hash, which forces a frontend redeploy. The `log` op raises
        # when the reader is dead, which is what actually protects a growth. See
        # docs/TODO.md.
        return ChamberLogReadout(
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

    def readout(self) -> ChamberConfigReadout:
        try:
            sections = self._sections()
        except Exception:
            sections = []
        return ChamberConfigReadout(
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

    def readout(self) -> MIModeReadout:
        executions = self.mi_server.list_executions()
        running = [e for e in executions if not e.get("is_execution_finished")]
        return MIModeReadout(
            num_executions=len(executions),
            current_execution=running[0]["commands_uuid"] if running else None,
        )


def _stamp(header: dict) -> dict:
    """A frame header as FrameHeader fields. The cameras write `time` as a *string*."""
    return {
        "time": float(header.get("time", 0.0)),
        "uuid": str(header.get("uuid", "")),
        "time_stamp": str(header.get("time_stamp", "")),
    }


class FiducialHandler:
    """Markers the operator draws on the chamber webcam, and the pixel statistics under
    them.

    The store and the stats worker are the domain objects (`lumi.base.camera.fiducials`);
    this only maps them onto the contract. Geometry lives on the node so that every
    client -- the browser overlay, a calibration notebook -- sees the same markers, and
    the statistics are computed once, next to the camera, rather than once per client.
    """

    def __init__(self, store, worker) -> None:
        self.store = store
        self.worker = worker

    async def list_markers(self, req: Empty) -> MarkerList:
        size = self.worker.frame_size()
        return MarkerList(
            frame_width=size[0] if size else None,
            frame_height=size[1] if size else None,
            markers=self.store.list(),
        )

    async def set_marker(self, req: FiducialMarker) -> Ack:
        self.store.set(req)
        log.info("fiducial marker %r set: %s", req.marker_id, req.shape.kind)
        return Ack()

    async def remove_marker(self, req: MarkerId) -> Ack:
        self.store.remove(req.marker_id)
        log.info("fiducial marker %r removed", req.marker_id)
        return Ack()

    async def marker_stats(self, req: Empty) -> MarkerStatsSample:
        latest = self.worker.latest_sample()
        if latest is None:
            raise RuntimeError("no frame has been measured yet")
        return _sample(*latest)

    async def marker_history(self, req: MarkerHistoryQuery) -> MarkerHistory:
        if self.store.get(req.marker_id) is None:
            raise KeyError(f"no marker {req.marker_id!r}")
        return MarkerHistory(
            marker_id=req.marker_id,
            samples=[
                MarkerPoint(**_stamp(header), stats=stats)
                for stats, header in self.worker.trace(req.marker_id, req.limit)
            ],
        )

    async def set_role(self, req: RoleAssignment) -> Ack:
        self.store.set_role(req.role, req.marker_id)
        log.info("fiducial role %r set to marker %r", req.role, req.marker_id)
        return Ack()

    async def remove_role(self, req: RoleQuery) -> Ack:
        self.store.remove_role(req.role)
        log.info("fiducial role %r removed", req.role)
        return Ack()

    async def list_roles(self, req: Empty) -> RoleMap:
        return RoleMap(roles=self.store.list_roles(), known=list(KNOWN_ROLES))

    async def next(self) -> MarkerStatsSample | None:
        try:
            stats, header = self.worker.samples.get_nowait()
        except queue.Empty:
            return None
        return _sample(stats, header)

    def readout(self) -> FiducialReadout:
        size = self.worker.frame_size()
        return FiducialReadout(
            marker_ids=[m.marker_id for m in self.store.list()],
            frame_width=size[0] if size else None,
            frame_height=size[1] if size else None,
            n_processed=self.worker.n_processed,
        )


def _sample(stats: dict, header: dict) -> MarkerStatsSample:
    return MarkerStatsSample(**_stamp(header), stats=stats)
