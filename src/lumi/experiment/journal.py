"""The step journal: what happened, written by the system rather than by a person.

Answers "when did the heater go on, who turned it on, and did it work" without anyone
keeping a lab notebook in sync. Two hooks feed it and nothing else writes to `step`:

- `MqServer._handle_request`, for ops that complete inside one RPC (heater on, gas in,
  a target change, a human answering a gate). One row, opened and closed around the
  handler call.
- `ExperimentHandler._start_task`, for the long-running ones (`to_temperature`,
  `perform_deposition`, ...). These return a TaskAck immediately and finish minutes or
  hours later, so the row is opened when the task starts and closed by the runner --
  which already computes exactly the `{"ok": ...}` payload this stores.

Steps chain per **chamber session**, not per sample: alignment, a gas change or a
warm-up with nothing loaded are real steps that belong to no specimen. `sample_id` then
tags the subset that did touch one.

Nothing here may break a growth. Every write is wrapped: a journal that cannot write
logs and gets out of the way, because losing the record of a deposition is bad and
failing the deposition to protect the record is worse.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from lumi.experiment.db import GrowthDB

log = logging.getLogger(__name__)

#: Params are stored as JSON, and a step's request can carry things json.dumps refuses
#: (numpy scalars from a notebook, an enum from a payload). Falling back to repr keeps
#: the row rather than losing the step to a serialisation error.
def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def _dumps(data: dict | None) -> str | None:
    if not data:
        return None
    try:
        return json.dumps({k: _jsonable(v) for k, v in data.items()})
    except Exception:  # pragma: no cover - _jsonable already handles the realistic cases
        return None


class StepJournal:
    """Append-only writer for the `step` table.

    `sample_resolver` is a zero-argument callable returning the sample_id the chamber
    is currently working on, or None. The journal cannot know this itself -- the
    experiment handler owns the current substrate and pixel -- and it is resolved per
    step rather than cached so a step lands on whatever sample was loaded at the time.
    """

    def __init__(
        self,
        db: GrowthDB,
        *,
        node_instance: str | None = None,
        sample_resolver: Callable[[], int | None | Awaitable[int | None]] | None = None,
    ) -> None:
        self.db = db
        self.node_instance = node_instance
        self.sample_resolver = sample_resolver
        self.session_id: int | None = None
        self._last_step_id: int | None = None

    # --- session ---------------------------------------------------------------

    async def open_session(self) -> int | None:
        try:
            self.session_id = await self.db.start_chamber_session(self.node_instance)
            self._last_step_id = None
        except Exception:
            log.exception("could not open a chamber session; steps will not be journaled")
            self.session_id = None
        return self.session_id

    async def close_session(self) -> None:
        if self.session_id is None:
            return
        try:
            await self.db.end_chamber_session(self.session_id)
        except Exception:
            log.exception("could not close chamber session %s", self.session_id)
        finally:
            self.session_id = None

    # --- steps -----------------------------------------------------------------

    async def _sample_id(self) -> int | None:
        if self.sample_resolver is None:
            return None
        try:
            result = self.sample_resolver()
            return await result if inspect.isawaitable(result) else result
        except Exception:
            log.exception("sample_resolver failed; journaling this step without a sample")
            return None

    async def begin(
        self,
        kind: str,
        *,
        params: dict | None = None,
        actor: str | None = None,
        source: str | None = None,
        step_uuid: str | None = None,
        started_at: float | None = None,
    ) -> int | None:
        """Open a step row. Returns its id, or None if the journal is unavailable --
        callers pass that straight back to `end`, which then does nothing."""
        try:
            step_id = await self.db.add_step(
                kind,
                step_uuid=step_uuid or uuid.uuid4().hex,
                started_at=started_at if started_at is not None else time.time(),
                session_id=self.session_id,
                parent_step_id=self._last_step_id,
                sample_id=await self._sample_id(),
                params=_dumps(params),
                actor=actor,
                source=source,
            )
        except Exception:
            log.exception("could not journal the start of step %r", kind)
            return None
        # Chained in start order. A long task stays open while short ops run inside
        # it, so "the previous step" means the one most recently begun, not the one
        # most recently finished -- otherwise a 27-minute ramp would adopt as its
        # parent something that happened during it.
        self._last_step_id = step_id
        return step_id

    async def end(
        self,
        step_id: int | None,
        *,
        ok: bool,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        if step_id is None:
            return
        try:
            await self.db.finish_step(
                step_id, ok=ok, ended_at=time.time(), result=_dumps(result), error=error
            )
        except Exception:
            log.exception("could not journal the end of step %s", step_id)
