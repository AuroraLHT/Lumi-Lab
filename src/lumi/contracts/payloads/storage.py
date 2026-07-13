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


class StorageState(ServerStateBase):
    is_storing: bool = False
    project_name: str | None = None
    path: str | None = None
    # Storage is a consumer of the other nodes, so it can be up but unable to
    # record because a source is down. Surface that rather than failing opaquely.
    deps_available: dict[str, bool] = {}
    n_frames: int | None = None
