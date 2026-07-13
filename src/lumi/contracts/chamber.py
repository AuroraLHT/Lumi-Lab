"""The Pascal chamber node: growth log, chamber config, MI mode, chamber camera."""

from __future__ import annotations

from .payloads.camera import CameraConfig, CameraState, ImageMeta
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
    doc="Chamber-facing webcam. Same contract as the RHEED camera -- which is why "
        "there is one camera handler and one generated camera client, not two.",
    state=CameraState,
    ops=(
        Op("image", Empty, ImageMeta, response_codec=Codec.NPY),
        Op("get_camera_config", Empty, CameraConfig),
        Op("update_camera_config", CameraConfig, Ack),
    ),
    stream=StreamSpec("frame", ImageMeta, codec=Codec.NPY),
)

CHAMBER = EquipmentContract(
    name="chamber",
    exchange="CHAMBER",
    version="2.0",
    capabilities=(LOG, CONFIG, MI_MODE, CAMERA),
)
