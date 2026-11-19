"""Who did what to the chamber: every chamber.pld operation this node handled.

pld-service knows only which gRPC client holds control -- "lumi-pascal" for everything
that comes through Lumi -- so the Deck could not tell an agent's RHEED change from the
operator's. The bus can: every request carries an `actor` header ("hliang16" from the
Deck, "mcp:<user>" from the agent) and the client's `message_source`.

This is the chamber node's step journal. MqServer already hands a handler's `journal`
every op the contract marks `journal=True` -- every chamber.pld operation, and none of
its readings -- with that actor and source (`begin`), then the outcome (`end`). The
experiment node's StepJournal writes growth.db, which is on the server host; this one
keeps the newest entries in memory and appends each finished one to a JSONL file on
the instrument PC, which it reads back at start so a restart keeps the history.
"""

from __future__ import annotations

import collections
import itertools
import json
import logging
import time
from pathlib import Path

from lumi.contracts.payloads.pld import PldActivity

log = logging.getLogger(__name__)

#: Entries kept in memory (and read back from the file at start).
KEEP = 500

#: How many ride on every state-stream message.
ON_STATE = 10

#: Past this size the file moves to `<name>.1.jsonl` (replacing the one before) and a
#: new one starts: a few weeks of operations, at a few hundred bytes each.
ROTATE_BYTES = 5_000_000


class ActivityLog:
    def __init__(self, path: str | Path | None = None, keep: int = KEEP) -> None:
        self.path = Path(path) if path is not None else None
        self.entries: collections.deque[PldActivity] = collections.deque(maxlen=keep)
        self._open: dict[int, tuple[PldActivity, float]] = {}
        latest = self._load()
        self._seq = itertools.count(latest + 1)
        self.latest_seq = latest

    def _load(self) -> int:
        if self.path is None or not self.path.exists():
            return 0
        latest = 0
        try:
            with self.path.open(encoding="utf-8") as f:
                for line in collections.deque(f, maxlen=self.entries.maxlen):
                    try:
                        entry = PldActivity.model_validate_json(line)
                    except ValueError:
                        continue  # a line cut short by a crash
                    self.entries.append(entry)
                    latest = max(latest, entry.seq)
        except OSError:
            log.exception("could not read the activity log %s; starting empty", self.path)
        return latest

    # --- the journal interface MqServer calls -------------------------------------

    async def begin(self, kind: str, *, params: dict | None = None, actor: str | None = None,
                    source: str | None = None, **_) -> int:
        seq = next(self._seq)
        self.latest_seq = seq
        entry = PldActivity(seq=seq, time=time.time(), actor=actor, source=source, op=kind,
                            params=params or {}, running=True)
        self.entries.append(entry)
        self._open[seq] = (entry, time.monotonic())
        return seq

    async def end(self, step_id: int | None, *, ok: bool, result: dict | None = None,
                  error: str | None = None) -> None:
        opened = self._open.pop(step_id, None) if step_id is not None else None
        if opened is None:
            return
        entry, started = opened
        result = result or {}
        # A PldReply's ok is the service's answer; a PldControlReply says granted, whether
        # the caller holds control afterwards: so a release succeeded when it is false.
        if entry.op == "release_control":
            answer = not result.get("granted", False)
        else:
            answer = result.get("ok", result.get("granted", True))
        entry.ok = bool(ok and answer)
        entry.message = error or str(result.get("message", ""))
        entry.duration_s = round(time.monotonic() - started, 3)
        entry.running = False
        self._write(entry)

    def _write(self, entry: PldActivity) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > ROTATE_BYTES:
                self.path.replace(self.path.with_suffix(".1" + self.path.suffix))
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry.model_dump(), separators=(",", ":")) + "\n")
        except OSError:
            log.exception("could not append to the activity log %s", self.path)

    # --- reading --------------------------------------------------------------------

    def recent(self, limit: int = 50, since_seq: int = 0) -> list[PldActivity]:
        newer = [e for e in self.entries if e.seq > since_seq]
        return [e.model_copy() for e in newer[-limit:]]
