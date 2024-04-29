#!/usr/bin/env python
import pika
import sys
import struct
import numpy as np
import cv2
import matplotlib.pyplot as plt

fig, ax = plt.subplots()

def decode_img(body, ):
    header_size = struct.calcsize("<HH")
    img_shape = struct.unpack("<HH", body[:header_size])
    img_flat = np.frombuffer(body[header_size:], dtype=np.int16 )
    return img_flat.reshape(img_shape)

if __name__ == "__main__":

    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host='localhost')
    )
    channel = connection.channel()

    channel.exchange_declare(exchange='RHEED', exchange_type='direct')

    # declare client only queue, you can think of each ASGI server is a client
    result = channel.queue_declare('', exclusive=True)
    queue_name = result.method.queue

    channel.queue_bind(
        exchange='RHEED', 
        queue=queue_name, 
        routing_key="image",
    )

    def callback(ch, method, properties, body):
        img = decode_img(body)
        print(f" [x] {method.routing_key}:{body[:10]} {img.shape}")
        # image = cv2.imread('/home/hliang16/Pictures/Wallpapers/IMG_20230518_111128.jpg') 
        # cv2.imshow('Original', image) 

        # print(method)
        # print(properties)
        # ax.imshow(img)
        # plt.pause(0.05)
        cv2.imshow("Stream", img.astype(np.uint8))
        if cv2.waitKey(1) == 27:
            raise ValueError("stop") 


    channel.basic_consume(
        queue=queue_name, on_message_callback=callback, auto_ack=True)

    channel.start_consuming()