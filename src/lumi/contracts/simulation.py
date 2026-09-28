"""The simulation node: what a RHEED pattern should look like, computed.

No equipment behind it -- a crystal structure, a surface cut, the beam and the
screen go in; a picture and its labelled spots come out. Its own node rather than
part of the RHEED camera's, so a heavy calculation never competes with the camera
thread for a core, and a lab without the camera can still run it.
"""

from __future__ import annotations

from .payloads.common import Ack, Empty
from .payloads.simulation import (
    RheedJpegMeta,
    RheedJpegRequest,
    RheedSimMeta,
    RheedSimRequest,
    RheedSimState,
    SaveStructure,
    StructureInfo,
    StructureList,
    StructureName,
)
from .spec import Capability, Codec, EquipmentContract, Kind, Op

RHEED_SIM = Capability(
    name="rheed_sim",
    kind=Kind.RPC,
    doc="Kinematic RHEED simulation from a crystal structure, its surface orientation, "
        "the beam energy and incidence, and the screen geometry.",
    state=RheedSimState,
    ops=(
        Op("list_structures", Empty, StructureList,
           doc="Built-in structures, then saved ones."),
        Op("get_structure", StructureName, StructureInfo,
           doc="One structure with every atom of its conventional cell."),
        Op("save_structure", SaveStructure, StructureInfo,
           doc="Keep a CIF or a manual cell under a name. It is parsed first, and not "
               "kept if it cannot be simulated."),
        Op("delete_structure", StructureName, Ack, doc="Delete a saved structure."),
        Op("rheed_spots", RheedSimRequest, RheedSimMeta,
           doc="Where every rod and spot lands on the screen, labelled, without the "
               "image: tens of milliseconds, for an overlay on the live camera."),
        Op("simulate_rheed", RheedSimRequest, RheedSimMeta, response_codec=Codec.NPY,
           doc="The simulated pattern as float32 in [0, 1], height x width, with the "
               "spot list. Lossless, for analysis; a browser wants simulate_rheed_jpeg."),
        Op("simulate_rheed_jpeg", RheedJpegRequest, RheedJpegMeta, response_codec=Codec.RAW,
           doc="The simulated pattern as a greyscale JPEG (the body), with the spot list."),
    ),
)

SIMULATION = EquipmentContract(
    name="simulation",
    exchange="SIMULATION",
    version="1.0",
    capabilities=(RHEED_SIM,),
)
