import struct
import numpy as np
import datetime
import io
import base64

from typing import Dict, Optional

FIX_HEADER_KEYS = set(["type", "shape", "dtype"])

def encode_arr(arr, headers, to_base64=False):
    buffer = io.BytesIO()
    np.save(buffer, arr, allow_pickle=False,)
    buffer.seek(0)
    body = buffer.read()
    if to_base64:
        body = base64.b64encode(body).decode('utf-8')
    return body, headers
    
def decode_arr(body, headers, from_base64=False):
    if from_base64:
        body = base64.b64decode(body)

    buffer = io.BytesIO(body)
    buffer.seek(0)
    return np.load(buffer, allow_pickle=False), headers

def encode_img(img:np.ndarray, img_header:Dict, to_base64=False):
    # if img.ndim == 2:
    #     img = img[..., None]
    return encode_arr(img, img_header, to_base64=to_base64)

def decode_img(body, headers, from_base64=False):
    return decode_arr(body, headers, from_base64=from_base64)


def encode_mask(mask, mask_header, to_base64=False):
    return encode_arr(mask, mask_header, to_base64=to_base64)

def decode_mask(mask, mask_header, from_base64=False):
    return decode_arr(mask, mask_header, from_base64=from_base64)

