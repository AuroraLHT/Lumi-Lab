import struct
import numpy as np
import datetime

from typing import Dict, Optional

IMG_DTYPE = np.uint16

# def encode_img(img:np.ndarray, timestamp:float):
#     print("image", img)
#     if img.ndim == 2:
#         img = img[..., None]

#     img = img.astype(IMG_DTYPE)
#     img_bytes = img.tobytes()
    
#     h, w, d = img.shape
#     header_bytes = struct.pack("<dHHH", timestamp, h, w, d)
#     body = header_bytes+img_bytes
#     return body

# def decode_img(body, ):
#     header_size = struct.calcsize("<dHHH")
#     header = struct.unpack("<dHHH", body[:header_size])
#     timestamp = header[0]
#     img_shape=header[1:4]
#     img_flat = np.frombuffer(body[header_size:], dtype=np.int16 )
#     return img_flat.reshape(img_shape), {'timestamp':timestamp, 'shape':img_shape}


def encode_img(img:np.ndarray, img_header:Dict):
    if img.ndim == 2:
        img = img[..., None]

    img = img.astype(IMG_DTYPE)
    body = img.tobytes()
    h, w, d = img.shape

    img_header = { k : str(v) for k, v in img_header.items()}
    
    headers = {
        "type" : "ArrayImage",
        "height" : f"{h}",
        "width" : f"{w}",        
        "dim" : f"{d}",
        "dtype" : "uint16",
        **img_header
    }

    return body, headers

FIX_HEADER_KEYS = set(["type", "height", "width", "dim", "dtype"])

def decode_img(body, headers):
    img_shape= ( int(headers['height']), int(headers['width']), int(headers['dim']) )
    dtype = np.uint16 if headers['dtype']=="uint16" else np.uint8
    img = np.frombuffer(body, dtype=dtype ).reshape(img_shape)
    img_headers = { k : headers[k] for k in set( headers.keys() ) - FIX_HEADER_KEYS }
    return img, img_headers
# datetime.datetime.fromisoformat(headers['time'])


def encode_masks(mask:np.ndarray, mask_header:Optional[Dict]=None):
    """
    mask is a binary array
    """

    body = mask.tobytes()
    n_mask, h, w = mask.shape

    if mask_header is not None:
        mask_header = { k : str(v) for k, v in mask_header.items()}
    else:
        mask_header = {}

    headers = {
        "type" : "ArrayImage",
        "height" : f"{h}",
        "width" : f"{w}",        
        "n_mask" : f"{n_mask}",
        "dtype" : "bool",
        **mask_header
    }

    return body, headers

FIX_MASKS_HEADER_KEYS = set(["type", "height", "width", "n_mask", "dtype"])

def decode_masks(body, headers):
    mask_shape= (int(headers['n_mask']), int(headers['height']), int(headers['width']) )
    dtype = "bool"
    mask = np.frombuffer(body, dtype=dtype ).reshape(mask_shape)
    mask_headers = { k : headers[k] for k in set( headers.keys() ) - FIX_MASKS_HEADER_KEYS }
    return mask, mask_headers
# datetime.datetime.fromisoformat(headers['time'])


def encode_mask(mask:np.ndarray, mask_header:Optional[Dict]=None):
    """
    mask is a binary array
    """

    body = mask.tobytes()
    h, w = mask.shape

    if mask_header is not None:
        mask_header = { k : str(v) for k, v in mask_header.items()}
    else:
        mask_header = {}

    headers = {
        "type" : "ArrayImage",
        "height" : f"{h}",
        "width" : f"{w}",        
        "dtype" : "bool",
        **mask_header
    }

    return body, headers

FIX_MASK_HEADER_KEYS = set(["type", "height", "width", "dtype"])

def decode_mask(body, headers):
    mask_shape= (int(headers['height']), int(headers['width']) )
    dtype = "bool"
    mask = np.frombuffer(body, dtype=dtype ).reshape(mask_shape)
    mask_headers = { k : headers[k] for k in set( headers.keys() ) - FIX_MASK_HEADER_KEYS }
    return mask, mask_headers
# datetime.datetime.fromisoformat(headers['time'])
