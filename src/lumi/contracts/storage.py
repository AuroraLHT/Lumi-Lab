"""The storage node: records the other nodes' streams into HDF5.

Storage is the odd one out -- an "equipment" with no equipment. It is a *consumer*
of RHEED and the chamber, so it can be perfectly healthy and still unable to
record because a source is down. StorageState.deps_available exists to say so out
loud rather than failing opaquely mid-growth.
"""

from __future__ import annotations

from .payloads.common import Empty
from .payloads.storage import StorageRequest, StorageState, StorageStatus
from .spec import Capability, EquipmentContract, Kind, Op

STORAGE = Capability(
    name="storage",
    kind=Kind.RPC,
    doc="Start and stop an HDF5 recording session.",
    state=StorageState,
    ops=(
        # Named `start_recording`, not `start`: an op called `start` generates a client
        # method that shadows CapabilityClient.start(), so connecting the client would
        # silently fire a request instead. The contract now rejects such names outright.
        Op("start_recording", StorageRequest, StorageStatus),
        Op("stop_recording", Empty, StorageStatus),
    ),
)

STORAGE_NODE = EquipmentContract(
    name="storage",
    exchange="STORAGE",
    version="2.0",
    capabilities=(STORAGE,),
)
