import struct
import numpy as np
import datetime
import io
import base64

from typing import Dict, Optional

# IMG_DTYPE = np.uint16

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
    return np.load(buffer), headers

def encode_img(img:np.ndarray, img_header:Dict, to_base64=False):
    if img.ndim == 2:
        img = img[..., None]
    return encode_arr(img, img_header, to_base64=to_base64)

def decode_img(body, headers, from_base64=False):
    return decode_arr(body, headers, from_base64=from_base64)

# def encode_img(img:np.ndarray, img_header:Dict):
#     if img.ndim == 2:
#         img = img[..., None]

#     img = img.astype(IMG_DTYPE)
#     body = img.tobytes()
#     h, w, d = img.shape

#     img_header = { k : str(v) for k, v in img_header.items()}
    
#     headers = {
#         "type" : "ArrayImage",
#         "height" : f"{h}",
#         "width" : f"{w}",        
#         "dim" : f"{d}",
#         "dtype" : "uint16",
#         **img_header
#     }

#     return body, headers



# def decode_img(body, headers):
#     img_shape= ( int(headers['height']), int(headers['width']), int(headers['dim']) )
#     dtype = np.uint16 if headers['dtype']=="uint16" else np.uint8
#     img = np.frombuffer(body, dtype=dtype ).reshape(img_shape)
#     img_headers = { k : headers[k] for k in set( headers.keys() ) - FIX_HEADER_KEYS }
#     return img, img_headers

# datetime.datetime.fromisoformat(headers['time'])

def encode_mask(mask, mask_header, to_base64=False):
    return encode_arr(mask, mask_header, to_base64=to_base64)

def decode_mask(mask, mask_header, from_base64=False):
    return decode_arr(mask, mask_header, from_base64=from_base64)

# def encode_masks(mask:np.ndarray, mask_header:Optional[Dict]=None):
#     """
#     mask is a binary array
#     """

#     body = mask.tobytes()
#     n_mask, h, w = mask.shape

#     if mask_header is not None:
#         mask_header = { k : str(v) for k, v in mask_header.items()}
#     else:
#         mask_header = {}

#     headers = {
#         "type" : "ArrayImage",
#         "height" : f"{h}",
#         "width" : f"{w}",        
#         "n_mask" : f"{n_mask}",
#         "dtype" : "bool",
#         **mask_header
#     }

#     return body, headers

# FIX_MASKS_HEADER_KEYS = set(["type", "height", "width", "n_mask", "dtype"])

# def decode_masks(body, headers):
#     mask_shape= (int(headers['n_mask']), int(headers['height']), int(headers['width']) )
#     dtype = "bool"
#     mask = np.frombuffer(body, dtype=dtype ).reshape(mask_shape)
#     mask_headers = { k : headers[k] for k in set( headers.keys() ) - FIX_MASKS_HEADER_KEYS }
#     return mask, mask_headers
# # datetime.datetime.fromisoformat(headers['time'])


# def encode_mask(mask:np.ndarray, mask_header:Optional[Dict]=None):
#     """
#     mask is a binary array
#     """

#     body = mask.tobytes()
#     h, w = mask.shape

#     if mask_header is not None:
#         mask_header = { k : str(v) for k, v in mask_header.items()}
#     else:
#         mask_header = {}

#     headers = {
#         "type" : "ArrayImage",
#         "height" : f"{h}",
#         "width" : f"{w}",        
#         "dtype" : "bool",
#         **mask_header
#     }

#     return body, headers

# FIX_MASK_HEADER_KEYS = set(["type", "height", "width", "dtype"])

# def decode_mask(body, headers):
#     mask_shape= (int(headers['height']), int(headers['width']) )
#     dtype = "bool"
#     mask = np.frombuffer(body, dtype=dtype ).reshape(mask_shape)
#     mask_headers = { k : headers[k] for k in set( headers.keys() ) - FIX_MASK_HEADER_KEYS }
#     return mask, mask_headers
# # datetime.datetime.fromisoformat(headers['time'])
