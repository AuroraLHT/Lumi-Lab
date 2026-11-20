"""pld-service messages -> the bus payloads in `lumi.contracts.payloads.pld`.

Generic over the descriptor rather than field by field: the payload models mirror the
proto's names, so a walk that turns floats' NaN into None, enums into their names and
maps into dicts covers every message, and a field added on both sides needs no code here.
"""

from __future__ import annotations

import math

from google.protobuf.descriptor import FieldDescriptor

from lumi.contracts.payloads.pld import PldChamberState, PldEvent

_FLOATS = (FieldDescriptor.TYPE_DOUBLE, FieldDescriptor.TYPE_FLOAT)


def _scalar(field: FieldDescriptor, value):
    if field.type in _FLOATS:
        return value if math.isfinite(value) else None
    if field.type == FieldDescriptor.TYPE_ENUM:
        return field.enum_type.values_by_number[value].name if value in field.enum_type.values_by_number else str(value)
    return value


def _is_map(field: FieldDescriptor) -> bool:
    return field.message_type is not None and field.message_type.GetOptions().map_entry


def to_dict(message) -> dict:
    """Every field, set or not (proto3 has no presence for scalars), as plain Python."""
    out = {}
    for field in message.DESCRIPTOR.fields:
        value = getattr(message, field.name)
        if _is_map(field):
            value_field = field.message_type.fields_by_name["value"]
            out[field.name] = {
                k: (to_dict(v) if value_field.message_type else _scalar(value_field, v))
                for k, v in value.items()
            }
        elif field.is_repeated:
            out[field.name] = [to_dict(v) if field.message_type else _scalar(field, v) for v in value]
        elif field.message_type is not None:
            out[field.name] = to_dict(value)
        else:
            out[field.name] = _scalar(field, value)
    return out


def chamber_state(message) -> PldChamberState:
    data = to_dict(message)
    data.pop("raw_words", None)  # deliberately not carried; see the payload module
    return PldChamberState.model_validate(data)


def event(message) -> PldEvent:
    return PldEvent.model_validate(to_dict(message))
