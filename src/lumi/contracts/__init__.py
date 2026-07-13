"""The contract: the single source of truth for everything on the wire.

Declare an operation here, once. The Python client, the websocket bridge, the
TypeScript client, and the server's dispatch table are all generated from it.
Nothing else re-spells an op name or a payload shape.
"""

from .registry import (
    EQUIPMENT,
    REGISTRY,
    canonical_dict,
    canonical_json,
    contract,
    contract_hash,
    iter_capabilities,
)
from .spec import (
    Capability,
    Codec,
    ContractError,
    EquipmentContract,
    Kind,
    Op,
    RoutingKeys,
    StreamSpec,
)

__all__ = [
    "Capability",
    "Codec",
    "ContractError",
    "EQUIPMENT",
    "EquipmentContract",
    "Kind",
    "Op",
    "REGISTRY",
    "RoutingKeys",
    "StreamSpec",
    "canonical_dict",
    "canonical_json",
    "contract",
    "contract_hash",
    "iter_capabilities",
]
