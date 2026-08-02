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


class JpegMeta(FrameHeader):
    """Headers for a JPEG-encoded frame. The body is the JFIF buffer (Codec.RAW).

    The sibling of ImageMeta for capabilities that stream for *viewing* rather than
    for analysis. It carries width/height rather than `shape`/`dtype` because the
    body is no longer an array: the browser hands it to `createImageBitmap` and never
    learns what the sensor's dtype was. Consumers that need the true pixels call the
    `image` op instead, which stays lossless NPY.
    """

    width: int
    height: int
    quality: int | None = None
    #: Greyscale sources are encoded as single-channel JPEG rather than widened to
    #: RGB, so a consumer that cares (a thumbnailer, a recorder) can tell them apart
    #: without decoding.
    channels: int = 3


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
    #: Frames the stream encoder has enqueued, on a capability whose stream is encoded
    #: rather than raw; None where the array itself goes on the wire.
    #:
    #: This is a liveness signal for the *encoder thread only*, and it is worth being
    #: precise about that because the obvious reading is wrong: the encoder drops the
    #: oldest frame when its queue is full, so with nothing draining at all it goes on
    #: counting at full rate. "n_encoded is advancing" therefore does not mean anyone is
    #: receiving anything -- pair it with n_dropped, which is what actually reveals a
    #: consumer that has stopped keeping up.
    n_encoded: int | None = None
    #: Frames evicted unsent because the consumer was not keeping up. Expected to climb
    #: while is_streaming is False (nobody has subscribed, so nothing drains the queue);
    #: climbing *while* is_streaming is True is the real fault signal.
    n_dropped: int | None = None


class CameraState(CameraReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the camera's readout.
    pass


class VideoReadout(BaseModel):
    n_fragments: int | None = None
    fragment_duration: float | None = None


class VideoState(VideoReadout, ServerStateBase):
    # Wire state = server lifecycle (ServerStateBase) + the compressor's readout.
    pass
