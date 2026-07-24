"""RHEED analysis payloads: box integration and its STFT.

IntegrationResult and FFTResult are ported from lumi.rheed.types, where they
existed as TypedDicts used *inside* the node but invisible to every client -- the
integrator's wire payload happened to be an IntegrationResult and nothing said so.
"""

from __future__ import annotations

from pydantic import BaseModel

from .common import ServerStateBase


class BBox(BaseModel):
    """An integration region, in pixels."""

    x: int
    y: int
    width: int
    height: int


class RegisterBBox(BaseModel):
    bbox_id: int
    bbox: BBox


class BBoxId(BaseModel):
    bbox_id: int


class BBoxList(BaseModel):
    bboxes: dict[int, BBox]


class IntegrationResult(BaseModel):
    """Pixel statistics over one bbox for one frame."""

    mean: float
    max: float
    min: float
    height: int
    width: int
    center_x: float
    center_y: float


class IntegrationHeader(BaseModel):
    time: float
    uuid: str
    time_stamp: str
    bbox_id: int | str
    integration_uuid: str


class IntegrationSample(BaseModel):
    """One streamed integration tick: every registered box for one frame."""

    header: IntegrationHeader
    results: dict[int, IntegrationResult]


class IntegrationCache(BaseModel):
    """The integrator's retained history for one bbox."""

    bbox_id: int
    samples: list[IntegrationSample] = []


class FFTResult(BaseModel):
    fft_freq: list[float]
    fft_mag: list[float]
    fft_phase: list[float]
    time_start: float
    time_end: float
    timestamp_start: str
    timestamp_end: str
    time_resolution: float


class STFTSample(BaseModel):
    bbox_id: int
    result: FFTResult


class STFTCache(BaseModel):
    bbox_id: int
    samples: list[STFTSample] = []


class IntegratorReadout(BaseModel):
    registered_bboxes: list[int] = []
    cache_size: int | None = None


class IntegratorState(IntegratorReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the integrator's readout.
    pass


class STFTReadout(BaseModel):
    registered_bboxes: list[int] = []
    window_size: int | None = None
    time_resolution: float | None = None


class STFTState(STFTReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the calculator's readout.
    pass
