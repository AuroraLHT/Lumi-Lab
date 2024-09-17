import json

def encode_json(data):
    return json.dumps(data).encode("utf-8")

def decode_json(data):
    # return json.loads(data.decode("utf-8"))
    return json.loads(data)
