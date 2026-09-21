"""Fiducial markers on the chamber webcam, and the pixel statistics under each one.

A marker is a region the operator draws once on the camera image -- a cross on the mask
edge, a circle or rectangle over the substrate, a polygon around a window. The node keeps the
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
    """A cross-hair that measures a *point*. It is drawn as a cross so it is easy to see
    and to place, but the statistics are those of a small patch centred on it -- the arms
    are display only."""

    kind: Literal["cross"]
    #: Centre of the cross -- the pixel at the middle of the measured patch.
    x: float
    y: float
    #: Half-length of each arm, in pixels. Display only.
    size: float = Field(default=10.0, gt=0)
    #: Half-width of the square patch the statistics cover: 0 is the single centre pixel,
    #: 1 (the default) a 3x3 patch, 2 a 5x5. One camera pixel is noisy, and the mean of a
    #: few is a steadier reading of the same place. A patch that hangs off the frame edge
    #: is clipped to it. `MarkerStats.center` is the raw centre pixel whatever this is.
    sample_radius: int = Field(default=1, ge=0, le=50)


class CircleShape(BaseModel):
    """A disc. Statistics cover the pixels whose centres fall inside it; one smaller than
    a pixel still measures the pixel it is over."""

    kind: Literal["circle"]
    #: Centre of the circle.
    x: float
    y: float
    #: Radius in pixels.
    radius: float = Field(gt=0)


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
Shape = Annotated[CrossShape | CircleShape | RectShape | PolyShape, Field(discriminator="kind")]


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
    #: Intensity of the single pixel at the marker's geometric centre -- the position for
    #: a cross or circle, the middle of a rectangle, the area centroid of a polygon --
    #: on the same scale as the statistics above. The same field on every shape, so a
    #: consumer that only wants a point reads this and need not care what was drawn. It is
    #: always the one raw pixel: for the steadier reading of a cross, which averages a
    #: patch around it, read `mean`. None when that pixel is off the frame.
    center: float | None = None


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
