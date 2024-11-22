import json
import struct
from typing import Union

from lumi.utils.common import decode_json
from .models import WebsocketMessageHeaders


def pack_headers(headers: dict) -> bytes:
    header_json = json.dumps(headers)
    header_length = struct.pack(">I", len(header_json))
    return header_length + header_json.encode("utf-8")


def pack_payload(payload: bytes, headers: dict) -> bytes:
    header_content = pack_headers(headers)
    return header_content + payload


def pack_websocket_payload(
    payload: bytes, headers: dict, websocket_headers: dict
) -> bytes:
    package_content = pack_payload(payload, headers)
    websocket_header_content = pack_headers(websocket_headers)
    return websocket_header_content + package_content


def update_state(body: bytes, state: dict) -> str:
    content: dict = json.loads(body)
    content.update(state)
    return json.dumps(content)


def unpack_header(message: bytes) -> tuple[WebsocketMessageHeaders, bytes]:
    size_format = ">I"
    size_bytes = struct.calcsize(size_format)

    header_length = struct.unpack(size_format, message[:size_bytes])[0]
    header = json.loads(message[size_bytes : size_bytes + header_length])

    message = message[size_bytes + header_length:]
    return header, message


def unpack_websocket_message(message: bytes) -> tuple[WebsocketMessageHeaders, bytes]:
    websocket_header, message = unpack_header(message)
    websocket_header = WebsocketMessageHeaders(**websocket_header)
    return websocket_header, message


def unpack_payload(message: bytes) -> tuple[dict, bytes]:
    headers, payload = unpack_header(message)
    return headers, payload


def unpack_websocket_payload(
    message: bytes,
) -> tuple[WebsocketMessageHeaders, dict, bytes]:
    websocket_header, payload_content = unpack_websocket_message(message)
    headers, payload = unpack_payload(payload_content)
    return websocket_header, headers, payload


def parse_payload(payload: bytes, websocket_headers: WebsocketMessageHeaders) -> Union[dict, str, bytes]:
    payload_type = websocket_headers.payload_type
    if payload_type == "json":
        return decode_json(payload)
    elif payload_type == "text":
        return payload.decode("utf-8")
    elif payload_type == "bytes":
        return payload
    else:
        raise ValueError(f"Unknown pyload type: {payload_type}")
