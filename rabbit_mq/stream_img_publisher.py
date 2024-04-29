import pika
# Importing the PIL library
import numpy as np
from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont
import datetime
import struct
import time

dt = np.int16

def generate_random_img():
    img = np.random.randint(0, 255, size=(540, 720), dtype=np.int16)
    
    # Call draw Method to add 2D graphics in an image
    pil_img = Image.fromarray(img)
    I1 = ImageDraw.Draw( pil_img )
     
    # Add Text to an image
    I1.text((150, 200), f"{datetime.datetime.now().time().isoformat()}", fill=(0), font=ImageFont.truetype("FreeMono.ttf", size=75))

    img = np.array(pil_img)
    img = img.astype(dt)
    return img

def encode_img(img):
     
    img_bytes = img.tobytes()
    
    h, w = img.shape
    hw_bytes = struct.pack("<HH", h, w)
   
    body = hw_bytes+img_bytes
    return body

if __name__ == "__main__":

    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host="localhost"),
    )

    channel = connection.channel()

    channel.exchange_declare('RHEED', exchange_type='direct')

    while True:
        img = generate_random_img()
        body = encode_img(img)
        channel.basic_publish(
            exchange='RHEED',
            routing_key="image",
            body = body
        )
        time.sleep(1)
