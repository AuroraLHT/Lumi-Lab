import json
import numpy as np

def convert_np_to_py(obj):
    if isinstance(obj, np.integer):
        return int(obj)
    elif isinstance(obj, np.floating):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    else:
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
    return obj

def encode_json(data):
    return json.dumps(data, default=convert_np_to_py).encode("utf-8")

def decode_json(data):
    # return json.loads(data.decode("utf-8"))
    return json.loads(data)
