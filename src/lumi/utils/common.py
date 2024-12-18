import json
import numpy as np
from typing import Any, Union, Dict, List
import datetime

def get_current_timestamp():
    current = datetime.datetime.now()
    return current.isoformat()

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

def encode_json(data: Union[Dict, List], encode=True):
    json_data = json.dumps(data, default=convert_np_to_py)
    if encode:
        return json_data.encode("utf-8")
    else:
        return json_data

def decode_json(data: Union[bytes, str]):
    # return json.loads(data.decode("utf-8"))
    if len(data) == 0:
        return {}
    return json.loads(data)
