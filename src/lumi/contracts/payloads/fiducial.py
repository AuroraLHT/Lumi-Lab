"""Fiducial markers on the chamber webcam, and the pixel statistics under each one.

A marker is a region the operator draws once on the camera image -- a cross on the mask
edge, a rectangle over the substrate, a polygon around a window. The node keeps the
list (and persists it), the frontend draws it over the stream, and a worker thread
reports mean / min / max / std of the pixels under every marker, frame by frame. The
statistics are the point: watching a marker's intensity while the mask moves is how the
mask's exact position is found, since the trace dips as the edge crosses the marker.

Everything is in **camera-frame pixels**, origin top-left, x to the right, y down --
the same space the MJPEG stream is encoded in, so a frontend scales one factor
(`displayed width / frame_width`) and draws the geometry as it is.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .common import FrameHeader, ServerStateBase


class Point(BaseModel):
    x: float
    y: float


class CrossShape(BaseModel):
    """A cross-hair. Statistics are taken over the pixels *on its two arms*, not the
    square they span -- a cross is a thin sampling line, so it can sit across an edge
    without the empty corners diluting the reading."""

    kind: Literal["cross"]
    #: Centre of the cross.
    x: float
    y: float
    #: Half-length of each arm, in pixels.
    size: float = Field(default=10.0, gt=0)
    #: Arm thickness in pixels. 1 samples a single pixel-wide line.
    thickness: int = Field(default=1, ge=1)


class RectShape(BaseModel):
    """An axis-aligned rectangle. `x`/`y` is the top-left corner, matching the RHEED
    `BBox`. Statistics cover the pixels inside it."""

    kind: Literal["rect"]
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class PolyShape(BaseModel):
    """A closed polygon, vertices in order. Statistics cover the pixels inside it."""

    kind: Literal["poly"]
    points: list[Point] = Field(min_length=3)


#: Tagged on `kind`, so a frontend narrows with a plain `switch (shape.kind)`.
Shape = Annotated[CrossShape | RectShape | PolyShape, Field(discriminator="kind")]


class FiducialMarker(BaseModel):
    """One named marker. Setting an id that already exists replaces it."""

    marker_id: str = Field(min_length=1, max_length=64)
    shape: Shape


class MarkerId(BaseModel):
    marker_id: str


class MarkerList(BaseModel):
    #: Size of the frames the markers are measured against, as last seen from the
    #: camera; None until the first frame arrives.
    frame_width: int | None = None
    frame_height: int | None = None
    markers: list[FiducialMarker] = []


class MarkerStats(BaseModel):
    """Intensity statistics over the pixels under one marker, for one frame.

    Colour frames are reduced to luminance first (Rec. 601), so the numbers are on the
    same scale as the sensor's own range. `std` is the population standard deviation.

    The four statistics are None -- with `n_pixels` 0 -- when the marker lies wholly
    outside the frame, which is a marker drawn for a different resolution rather than a
    reading of zero.
    """

    n_pixels: int
    mean: float | None = None
    min: float | None = None
    max: float | None = None
    std: float | None = None


class MarkerStatsSample(FrameHeader):
    """Statistics for every marker on one frame; the header is that frame's."""

    stats: dict[str, MarkerStats]


class MarkerHistoryQuery(BaseModel):
    marker_id: str
    #: Most recent N samples; omitted for everything retained.
    limit: int | None = Field(default=None, ge=1)


class MarkerPoint(FrameHeader):
    """One marker's statistics at one frame -- a point on its intensity trace."""

    stats: MarkerStats


class MarkerHistory(BaseModel):
    """The retained trace for one marker, oldest first."""

    marker_id: str
    samples: list[MarkerPoint] = []


class FiducialReadout(BaseModel):
    marker_ids: list[str] = []
    frame_width: int | None = None
    frame_height: int | None = None
    #: Frames whose statistics have been computed. Advancing means the worker is alive.
    n_processed: int | None = None


class FiducialState(FiducialReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the worker's readout.
    pass
