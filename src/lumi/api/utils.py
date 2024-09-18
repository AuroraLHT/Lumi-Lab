import json
import struct

def package_payload(payload : bytes, headers : dict) -> bytes:
    header_json = json.dumps(headers)
    header_length = struct.pack(">I", len(header_json))
    return header_length + header_json.encode("utf-8") + payload

def update_state(body : bytes, state : dict) -> str:
    content : dict = json.loads(body)
    content.update(state)
    return json.dumps(content)
