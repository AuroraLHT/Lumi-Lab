import pika
# Importing the PIL library
import numpy as np
from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont
import datetime
import struct
import time
import argparse

dt = np.int16
default_fonts = ["FreeMono.ttf", "arial.ttf"]
default_font = None

def generate_random_img():
    img = np.random.randint(0, 255, size=(540, 720), dtype=np.int16)
    
    # Call draw Method to add 2D graphics in an image
    pil_img = Image.fromarray(img)
    I1 = ImageDraw.Draw( pil_img )
     
    # Add Text to an image
    I1.text((150, 200), f"{datetime.datetime.now().time().isoformat()}", fill=(0), font=ImageFont.truetype(default_font, size=75))

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
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost", type=str)
    args = parser.parse_args()

    host = args.host


    for font in default_fonts:
        try:
            ImageFont.truetype(font, size=75)
            default_font = font
        except Exception as e:
            print(e)
        if default_font is not None:
            break

    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host=host),
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
