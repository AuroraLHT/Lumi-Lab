# import pika
# import pika.channel

# Importing the PIL library

import asyncio
from aio_pika import Message, Channel, Exchange
from aio_pika.abc import (
    AbstractChannel, AbstractExchange,  AbstractConnection, AbstractIncomingMessage, AbstractQueue,
)
import numpy as np
import datetime
import struct
import time
import argparse
import logging

from video_stream import VideoCompressor
import json

from misc import encode_img

# IMG_DTYPE = np.int16

# def encode_img(img, timestamp):
#     img = img.astype(IMG_DTYPE)
#     img_bytes = img.tobytes()
    
#     h, w = img.shape
#     header_bytes = struct.pack("<dHH", timestamp, h, w)
#     body = header_bytes+img_bytes
#     return body


class ImageMessageQueue:
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    queue : AbstractQueue

    def __init__(self, camera, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str):
        super().__init__()
        self.channel = channel
        self.exchange = exchange
        self.callback_exchange = channel.default_exchange
        self.routing_key = routing_key
        self.camera = camera
        self.queue = None

    async def create_queue(self):
        logging.info(" [x] Create temporary queue")

        self.queue = await self.channel.declare_queue(exclusive=True)
        await self.queue.bind(self.exchange, routing_key=self.routing_key)

    async def on_message(self, message: AbstractIncomingMessage):
        logging.info("On Image ")
        async with message.process():
            img, time_stamp = self.camera.get_frame() # this get the latest frame from the peek queue
            body, headers = encode_img(img, time_stamp)

            await self.callback_exchange.publish(
                Message(
                    body = body,
                    correlation_id=message.correlation_id,
                    headers=headers,
                ),
                routing_key=message.reply_to
            )
        logging.info("Send Image ")

            # the example didn't ack back if process() method is used
            # await message.ask()


    async def start(self):
        await self.create_queue()

        logging.info(" [x] Awaiting Camera Frame requests")
        self._consume_tag = await self.queue.consume(self.on_message, no_ack=False)
        # async with self.queue.iterator() as qiterator:
        #     message : AbstractIncomingMessage
        #     async for message in qiterator:
        #         try:
        #             await self.on_message(message=message)
        #         except Exception:
        #             logging.exception("Processing error for message %r", message)


class LiveImageMessageQueue:
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    control_routing_key : str
    publish_routing_key : str

    queue : AbstractQueue
    control_queue : AbstractQueue

    def __init__(self, camera, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, control_routing_key:str, publish_routing_key:str):
        """
            has no input, routing_key could be None
        """
        super().__init__()
        self.channel = channel
        self.exchange = exchange
        self.callback_exchange = channel.default_exchange
        self.routing_key = routing_key
        self.publish_routing_key = publish_routing_key
        self.control_routing_key = control_routing_key
        self.camera = camera

        self.queue = None
        self.control_queue = None

        self.fps = camera.config.fps
        self.spf = 1 / self.fps

    async def create_queue(self):
        logging.info(" [x] Create temporary queue")

        # self.queue = await self.channel.declare_queue(exclusive=True)
        # await self.queue.bind(self.exchange, routing_key=self.routing_key)

        self.control_queue = await self.channel.declare_queue(exclusive=True)
        await self.control_queue.bind(self.exchange, routing_key=self.routing_key)


    async def on_control_message(self, message: AbstractIncomingMessage):
        logging.info("On Control Message")
        async with message.process():
            body, headers = message.body, message.headers
            body = json.loads( body )
            if 'fps' in body:
                self.fps = int(body['fps'])
                self.spf = 1 / self.fps

        logging.info("Off Control Message")

            # the example didn't ack back if process() method is used
            # await message.ask()

    async def publish(self,):
        prev_current_time = time.time()
        while True:
            current_time = time.time()
            if current_time - prev_current_time > self.spf:
                img, time_stamp = self.camera.get_frame() # this get the latest frame from the peek queue
                body, headers = encode_img(img, time_stamp)

                await self.callback_exchange.publish(
                    Message(
                        body = body,
                        headers=headers,
                    ),
                    routing_key=self.publish_routing_key
                )
                print("Send Live Image ")
            else:
                await asyncio.sleep( self.spf / 10)
            prev_current_time = current_time



    async def start(self):
        await self.create_queue()
        logging.info(" [x] Start Live Image Message Queue")
        await self.control_queue.consume(self.on_control_message, no_ack=False)
        self._publish_task = asyncio.create_task( self.publish() )


class VideoFragmentsMessageQueue:
    video_compressor : VideoCompressor
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    queue : AbstractQueue

    def __init__(self, video_compressor:VideoCompressor, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str):
        super().__init__()
        self.channel = channel
        self.exchange = exchange
        self.callback_exchange = self.channel.default_exchange
        self.routing_key = routing_key
        self.video_compressor = video_compressor
        self.queue = None

    async def create_queue(self):
        logging.info(" [x] Create temporary queue")

        self.queue = await self.channel.declare_queue(exclusive=True)        
        await self.queue.bind(exchange=self.exchange, routing_key=self.routing_key)

    async def on_message(self, message: AbstractIncomingMessage):
        async with message.process():
            assert message.reply_to is not None

            body = message.body.decode()
            headers = message.headers
            # print(body, headers)

            logging.info(f'Video Initial Queue receive message: {body}')
            fragment_idx = int(body)
            logging.info(f"get fragment {fragment_idx}")

            if headers["type"] == "initial":
                num_fragments = self.video_compressor.number_of_startup_fragments() # this get the latest frame from the peek queue
            else:
                num_fragments = 1


            fragment, headers = self.video_compressor.get_history_fragment(fragment_idx)
            logging.info(f"fragment length {len(fragment)}")
            await self.callback_exchange.publish(
                Message(
                    body=fragment,
                    headers= {
                        "size" : num_fragments,
                        "index" : fragment_idx,
                        **headers
                    },
                    correlation_id=message.correlation_id
                ),
                routing_key=message.reply_to
            )
            # print("Message send!")

    async def start(self):
        await self.create_queue()

        logging.info(" [x] Awaiting Video Fragment Request")
        self._consume_tage = await self.queue.consume(self.on_message, no_ack=False)
        # async with self.queue.iterator() as qiterator:
        #     message : AbstractIncomingMessage
        #     async for message in qiterator:
        #         try:
        #             await self.on_message(message=message)
        #         except Exception:
        #                 logging.exception("Processing error for message %r", message)


class VideoMessageQueue:
    video_compressor : VideoCompressor
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    publish_routing_key : str
    queue : AbstractQueue

    def __init__(self, video_compressor:VideoCompressor, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, publish_routing_key:str):
        super().__init__()
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.publish_routing_key = publish_routing_key
        self.video_compressor = video_compressor
    
    async def create_queue(self):        
        self.queue = self.channel.declare_queue(exclusive=True)
        await self.queue.bind(exchange=self.exchange, routing_key=self.routing_key)

    async def publish(self):

        while True:
            fragment, headers = self.video_compressor.get_fragment()
            await self.exchange.publish(
                message=Message(body=fragment, headers=headers),
                routing_key= self.publish_routing_key
            )
            # logging.info(f"publish fragment {headers['frag_idx']} frame start from {headers['frame_start']} to {headers['frame_end']}")

    async def start(self):
        logging.info(" [x] Start Video Stream")
        self._publish_task = asyncio.create_task( self.publish() )
        logging.info(" [x] Start Video Stream end")        
            # await asyncio.sleep(0.0001)

