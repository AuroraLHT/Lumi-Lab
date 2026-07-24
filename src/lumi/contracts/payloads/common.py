"""Payload models shared across equipment."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Empty(BaseModel):
    """No payload. Used for ops that take or return nothing."""


class Ack(BaseModel):
    """A bare success acknowledgement."""

    ok: bool = True


class OperationStatus(BaseModel):
    """Result of an operation that can fail in a domain-specific way.

    Distinct from a transport error: an op that returns OperationStatus(ok=False)
    was delivered, understood, and executed -- it just didn't work (the project
    name was taken, the bbox didn't exist). Transport failures come back as an
    error response instead.
    """

    ok: bool
    message: str = ""


class FrameHeader(BaseModel):
    """Metadata accompanying a camera frame.

    Ported from lumi.rheed.types.FrameHeader, which was a TypedDict that no
    client could see.
    """

    time: float
    uuid: str
    time_stamp: str
    frame_idx: int | None = None


class ServerStateBase(BaseModel):
    """The lifecycle fields the *server* owns, carried by every capability's state.

    The old code kept state in a free-form dict, so consumers read it by string
    (`camera_state["frame_dims"]`) and a typo was a KeyError at runtime, in the
    lab, mid-experiment.

    Ownership is split by type. These three fields are the server's: whether the
    capability is serving (`is_running`), whether its stream is emitting
    (`is_streaming`), and whether it is degraded (`error`) are transport lifecycle,
    not equipment readings. Everything a handler observes about its hardware lives
    on a sibling `*Readout` model, and the published `*State` is the union:

        class CameraReadout(BaseModel):        # what the handler reports
            frame_dims: list[int] | None = None
        class CameraState(ServerStateBase, CameraReadout):  # what goes on the wire
            ...

    The handler returns a `*Readout` (no lifecycle fields), so `refresh_state` can
    fold its readings in without ever clobbering the server-owned flags -- the
    readout's type simply has no `is_streaming` to overwrite it with.
    """

    is_running: bool = False
    is_streaming: bool = False
    error: str | None = Field(default=None, description="Last error, if the capability is degraded")
