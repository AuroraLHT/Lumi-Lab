"""Camera and video payloads. Shared by the RHEED and chamber nodes -- both run
cameras, and before this they each had their own byte-identical copy of the
camera client."""

from __future__ import annotations

from pydantic import BaseModel

from .common import FrameHeader, ServerStateBase


class CameraConfig(BaseModel):
    """Tunable camera parameters.

    Optional throughout: `update_camera_config` is a partial update, and the old
    dict-based version relied on the caller simply omitting keys.
    """

    exposure_time: float | None = None  # microseconds
    gain: float | None = None  # 0-240
    gamma: float | None = None  # 0-3.99
    black_level: float | None = None  # 0-511
    auto_exposure: bool | None = None
    auto_gain: bool | None = None
    fps: float | None = None
    width: int | None = None
    height: int | None = None


class ImageMeta(FrameHeader):
    """Headers for a frame. The body is the np.save buffer (Codec.NPY)."""

    dtype: str | None = None
    shape: list[int] | None = None


class FragmentIndex(BaseModel):
    fragment_idx: int


class FragmentsSize(BaseModel):
    size: int


class VideoFragmentMeta(BaseModel):
    """Headers for an encoded video fragment. Body is the raw container (Codec.RAW)."""

    fragment_idx: int | None = None
    time: float | None = None
    time_stamp: str | None = None


class InitialFragmentsMeta(BaseModel):
    """The startup fragments a player must append before any media fragment.

    There are several of them, and the body is their concatenation -- `sizes` says
    where to cut. The old wire format base64'd each one into a JSON dict, which
    inflated the whole init segment by a third for no reason; a media-source player
    appends them back-to-back regardless.
    """

    count: int
    sizes: list[int]
    headers: list[dict[str, object]] = []


class CameraReadout(BaseModel):
    frame_dims: list[int] | None = None
    frame_metas: dict[str, object] | None = None
    fps: float | None = None


class CameraState(CameraReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the camera's readout.
    pass


class VideoReadout(BaseModel):
    n_fragments: int | None = None
    fragment_duration: float | None = None


class VideoState(VideoReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the compressor's readout.
    pass
