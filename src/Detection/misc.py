import struct
import numpy as np
import datetime

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


def encode_img(img:np.ndarray, timestamp:float):
    if img.ndim == 2:
        img = img[..., None]

    img = img.astype(IMG_DTYPE)
    body = img.tobytes()    
    h, w, d = img.shape

    headers = {
        "type" : "ArrayImage",
        "time": f"{datetime.datetime.fromtimestamp(timestamp)}",
        "height" : f"{h}",
        "width" : f"{w}",        
        "dim" : f"{d}",
        "dtype" : "uint16"
    }

    return body, headers

def decode_img(body, headers):
    img_shape= ( int(headers['height']), int(headers['width']), int(headers['dim']) )
    dtype = np.uint16 if headers['dtype']=="uint16" else np.uint8
    img = np.frombuffer(body, dtype=dtype ).reshape(img_shape)
    return img, datetime.datetime.fromisoformat(headers['time'])
