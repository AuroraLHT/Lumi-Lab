"""The RHEED node: camera, video encoding, box integration, STFT."""

from __future__ import annotations

from .payloads.camera import (
    CameraConfig,
    CameraState,
    FragmentIndex,
    FragmentsSize,
    ImageMeta,
    InitialFragmentsMeta,
    VideoFragmentMeta,
    VideoState,
)
from .payloads.common import Ack, Empty
from .payloads.rheed import (
    BBoxId,
    BBoxList,
    IntegrationCache,
    IntegrationSample,
    IntegratorState,
    RegisterBBox,
    STFTCache,
    STFTSample,
    STFTState,
)
from .spec import Capability, Codec, EquipmentContract, Kind, Op, StreamSpec

CAMERA = Capability(
    name="camera",
    kind=Kind.DUPLEX,
    doc="RHEED camera: pull a frame on demand, or subscribe to the live feed.",
    state=CameraState,
    ops=(
        Op("image", Empty, ImageMeta, response_codec=Codec.NPY, doc="Latest frame."),
        Op("get_camera_config", Empty, CameraConfig),
        Op("update_camera_config", CameraConfig, Ack, doc="Partial update; omitted fields are left alone."),
    ),
    stream=StreamSpec("frame", ImageMeta, codec=Codec.NPY),
)

VIDEO = Capability(
    name="video",
    kind=Kind.DUPLEX,
    doc="H.264-encoded fragments of the camera feed, for browser playback.",
    state=VideoState,
    ops=(
        Op("initial_fragments", Empty, InitialFragmentsMeta, response_codec=Codec.RAW,
           doc="The startup fragments a player must append before any media fragment. "
               "Body is their concatenation; `sizes` says where to cut."),
        Op("initial_fragments_size", Empty, FragmentsSize),
        Op("video_fragment", FragmentIndex, VideoFragmentMeta, response_codec=Codec.RAW),
    ),
    stream=StreamSpec("fragment", VideoFragmentMeta, codec=Codec.RAW),
)

INTEGRATOR = Capability(
    name="integrator",
    kind=Kind.DUPLEX,
    doc="Integrates pixel intensity over registered boxes, frame by frame.",
    state=IntegratorState,
    ops=(
        Op("register", RegisterBBox, Ack),
        Op("remove", BBoxId, Ack),
        Op("bboxes", Empty, BBoxList),
        Op("cache", BBoxId, IntegrationCache),
    ),
    stream=StreamSpec("integration", IntegrationSample),
)

STFT = Capability(
    name="stft",
    kind=Kind.DUPLEX,
    doc="Short-time Fourier transform of each box's integration series -- this is "
        "what makes RHEED oscillations legible during growth.",
    state=STFTState,
    ops=(
        Op("register", RegisterBBox, Ack),
        Op("remove", BBoxId, Ack),
        Op("bboxes", Empty, BBoxList),
        Op("cache", BBoxId, STFTCache),
    ),
    stream=StreamSpec("stft", STFTSample),
)

RHEED = EquipmentContract(
    name="rheed",
    exchange="RHEED",
    version="2.0",
    capabilities=(CAMERA, VIDEO, INTEGRATOR, STFT),
)
