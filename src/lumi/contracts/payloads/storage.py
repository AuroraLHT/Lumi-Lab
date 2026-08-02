"""Storage payloads.

StorageRequest previously existed in three hand-maintained copies that had already
drifted: the pydantic model in api/models.py (no force_rewrite), the
StorageMessageQueueClient.start_storage signature (has it), and the dict the
server destructured -- which had to defensively backfill missing keys. One model now.
"""

from __future__ import annotations

from pydantic import BaseModel

from .common import ServerStateBase


class StorageRequest(BaseModel):
    project_name: str
    save_frame: bool = False
    save_ai: bool = False
    save_log: bool = False
    save_integration: bool = False
    force_rewrite: bool = False


class StorageStatus(BaseModel):
    ok: bool
    message: str = ""
    project_name: str | None = None
    path: str | None = None


class StorageReadout(BaseModel):
    is_storing: bool = False
    project_name: str | None = None
    path: str | None = None
    # Storage is a consumer of the other nodes, so it can be up but unable to
    # record because a source is down. Surface that rather than failing opaquely.
    deps_available: dict[str, bool] = {}
    # There is deliberately no frame counter here. `n_frames` used to be declared and
    # never assigned, so every heartbeat advertised a number that was always null and
    # every client had to write code for a value that never arrived.
    #
    # A progress count is still wanted, but not on this model: capability state rides
    # the 2s heartbeat, and `NodeRegistry.on_heartbeat` emits `state_changed` whenever
    # the blob differs from the previous one. Every field here is stable for the
    # duration of a recording, so events fire on real transitions only. A value that
    # ticks every 2s would turn the registry into a 0.5 Hz event pump, waking every
    # connected client for the length of a growth. When the counter is built it should
    # ride the getState() control call and be polled by whoever is actually looking.


class StorageState(StorageReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the recorder's readout.
    pass
