"""The Pascal chamber node: growth log, chamber config, MI mode, chamber camera."""

from __future__ import annotations

from .payloads.camera import CameraConfig, CameraState, ImageMeta, JpegMeta
from .payloads.chamber import (
    AllConfigs,
    ChamberConfigState,
    ChamberLogState,
    ConfigEntry,
    ConfigQuery,
    ConfigSection,
    ConfigSections,
    LogBatch,
    LogEntry,
    MICommands,
    MIExecution,
    MIExecutionList,
    MIModeState,
    SectionQuery,
)
from .payloads.common import Ack, Empty
from .spec import Capability, Codec, EquipmentContract, Kind, Op, StreamSpec

LOG = Capability(
    name="log",
    kind=Kind.DUPLEX,
    doc="The chamber's growth log, tailed from disk.",
    state=ChamberLogState,
    ops=(Op("log", Empty, LogBatch, doc="Latest log rows."),),
    stream=StreamSpec("log", LogEntry),
)

CONFIG = Capability(
    name="config",
    kind=Kind.RPC,
    doc="PLDconfig.ini, parsed and watched for changes.",
    state=ChamberConfigState,
    ops=(
        Op("get_all_config", Empty, AllConfigs),
        Op("get_config", ConfigQuery, ConfigEntry),
        Op("get_configs_by_section", SectionQuery, ConfigSection),
        Op("get_sections", Empty, ConfigSections),
    ),
)

MI_MODE = Capability(
    name="mi_mode",
    kind=Kind.PUBSUB,
    doc="Machine-instruction mode: submit a command script, get an immediate "
        "acknowledgement, and receive the execution result later on the update "
        "channel, correlated by commands_uuid. The one genuinely pub/sub capability.",
    state=MIModeState,
    ops=(
        Op("register_commands", MICommands, MIExecution,
           doc="Queue a script. Returns immediately; completion arrives as an update."),
        Op("list_execution", Empty, MIExecutionList),
    ),
    update=StreamSpec("execution", MIExecution),
)

CAMERA = Capability(
    name="camera",
    kind=Kind.DUPLEX,
    doc="Chamber-facing webcam. Shares the RHEED camera's ops -- which is why there "
        "is one camera handler and one generated camera client, not two -- but "
        "streams MJPEG rather than raw arrays.",
    state=CameraState,
    ops=(
        # Lossless, and deliberately so: this is the op to call when the pixels are
        # the point (a saved still, a measurement). The *stream* is for looking at.
        Op("image", Empty, ImageMeta, response_codec=Codec.NPY),
        Op("get_camera_config", Empty, CameraConfig),
        Op("update_camera_config", CameraConfig, Ack),
    ),
    # MJPEG, not raw arrays. At 640x480x3 and 25fps the NPY stream was ~23 MB/s per
    # subscriber, which saturated the websocket bridge and cost the browser a full
    # decode-and-widen on the main thread for every frame.
    #
    # JPEG rather than H.264 (which is what rheed.video does) because every stage of
    # this pipeline drops frames on purpose -- the camera's fan-out queues, the
    # bridge's per-subscription prefetch, and the frontend's keep-only-the-newest
    # buffer. Dropping an independent JPEG costs one frame; dropping an H.264
    # fragment corrupts playback until the next keyframe, and dropping an init
    # segment means the player never starts at all. It also keeps a subscriber
    # stateless (no init segment to fetch first, so late joiners just work), keeps
    # each frame's uuid/time_stamp aligned 1:1 with its pixels, and has no muxer
    # timeline to desynchronise -- the exact failure that killed the RHEED encoder
    # thread with a non-monotonic DTS.
    stream=StreamSpec("frame", JpegMeta, codec=Codec.RAW),
)

CHAMBER = EquipmentContract(
    name="chamber",
    exchange="CHAMBER",
    version="2.0",
    capabilities=(LOG, CONFIG, MI_MODE, CAMERA),
)
