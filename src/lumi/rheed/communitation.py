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

from .video_stream import VideoCompressor, VideoRecorder
import json

from ..utils.image import encode_img
from ..base.message_queue import BasicServer, BasicStreamServer, BasicStreamClient, BasicClient
from .pylon_camera import PylonCamera
from .web_camera import WebCamera

import queue

from typing import Union, List, Dict, Any, Callable
# IMG_DTYPE = np.int16

# def encode_img(img, timestamp):
#     img = img.astype(IMG_DTYPE)
#     img_bytes = img.tobytes()
    
#     h, w = img.shape
#     header_bytes = struct.pack("<dHH", timestamp, h, w)
#     body = header_bytes+img_bytes
#     return body

class CameraMessageQueueServer(BasicServer):
    camera : Union[PylonCamera, WebCamera]

    def __init__(self, camera, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, control_routing_key:str, server_name:str):
        super().__init__(channel=channel, exchange=exchange, control_routing_key=control_routing_key, routing_key=routing_key, server_name=server_name)

        self.camera = camera

    async def on_message(self, message):
        img, img_header = self.camera.get_frame() # this get the latest frame from the peek queue
        body, headers = encode_img(img, img_header)

        return body, headers

class CameraMessageQueueClient(BasicClient):
    async def get(self):
        logging.info(f"{self.client_name} get live image")

        headers = {
            "type":"image"
        }

        return await super().get(message=''.encode(), headers=headers)

# class ImageMessageQueue:
#     channel : AbstractChannel
#     exchange : AbstractExchange
#     routing_key : str
#     queue : AbstractQueue

#     def __init__(self, camera, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str) :
#         super().__init__()
#         self.channel = channel
#         self.exchange = exchange
#         self.callback_exchange = channel.default_exchange
#         self.routing_key = routing_key
#         self.camera = camera
#         self.queue = None

#     async def create_queue(self):
#         logging.info(" [x] Create temporary queue")

#         self.queue = await self.channel.declare_queue(exclusive=True)
#         await self.queue.bind(self.exchange, routing_key=self.routing_key)

#     async def on_message(self, message: AbstractIncomingMessage):
#         logging.info("On Image ")
#         async with message.process():
#             img, img_header = self.camera.get_frame() # this get the latest frame from the peek queue
#             body, headers = encode_img(img, img_header)

#             await self.callback_exchange.publish(
#                 Message(
#                     body = body,
#                     correlation_id=message.correlation_id,
#                     headers=headers,
#                 ),
#                 routing_key=message.reply_to
#             )
#         logging.info("Send Image ")

#             # the example didn't ack back if process() method is used
#             # await message.ask()


#     async def start(self):
#         await self.create_queue()

#         logging.info(" [x] Awaiting Camera Frame requests")
#         self._consume_tag = await self.queue.consume(self.on_message, no_ack=False)
#         # async with self.queue.iterator() as qiterator:
#         #     message : AbstractIncomingMessage
#         #     async for message in qiterator:
#         #         try:
#         #             await self.on_message(message=message)
#         #         except Exception:
#         #             logging.exception("Processing error for message %r", message)

class LiveCameraMessageQueueServer(BasicStreamServer):
    camera : Union[PylonCamera, WebCamera]
    camera_queue : queue.Queue

    def __init__(
            self, 
            camera, 
            channel:AbstractChannel, 
            exchange:AbstractExchange, 
            control_routing_key:str, 
            publish_routing_key:str, 
            server_name:str
        ):
        """
            has no input, routing_key could be None
        """
        super().__init__(
            channel=channel, 
            exchange=exchange, 
            control_routing_key=control_routing_key, 
            publish_routing_key=publish_routing_key, 
            server_name=server_name
        )

        self.camera = camera

    async def on_message(self):
        if not self.camera_queue.empty():
            img, img_header = self.camera.get_frame() # this get the latest frame from the peek queue
            body, headers = encode_img(img, img_header)
            return body, headers
        else:
            return None, None

class LiveCameraMessageQueueClient(BasicStreamClient):
    def __init__(
            self, 
            channel: AbstractChannel, 
            exchange: AbstractExchange, 
            routing_key: str, 
            control_routing_key: str, 
            on_response_callback: Callable[..., Any], 
            client_name: str, 
            time_out: float
        ) -> None:

        super().__init__(
            channel, 
            exchange, 
            routing_key, 
            control_routing_key, 
            on_response_callback, 
            client_name, 
            time_out
        )

# class LiveImageMessageQueue:
#     channel : AbstractChannel
#     exchange : AbstractExchange
#     routing_key : str
#     control_routing_key : str
#     publish_routing_key : str

#     queue : AbstractQueue
#     control_queue : AbstractQueue

#     def __init__(self, camera, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, control_routing_key:str, publish_routing_key:str):
#         """
#             has no input, routing_key could be None
#         """
#         super().__init__()
#         self.channel = channel
#         self.exchange = exchange
#         self.callback_exchange = channel.default_exchange
#         self.routing_key = routing_key
#         self.publish_routing_key = publish_routing_key
#         self.control_routing_key = control_routing_key
#         self.camera = camera

#         self.queue = None
#         self.control_queue = None

#         self.fps = camera.config.fps
#         self.spf = 1 / self.fps

#     async def create_queue(self):
#         logging.info(" [x] Create temporary queue")

#         # self.queue = await self.channel.declare_queue(exclusive=True)
#         # await self.queue.bind(self.exchange, routing_key=self.routing_key)

#         self.control_queue = await self.channel.declare_queue(exclusive=True)
#         await self.control_queue.bind(self.exchange, routing_key=self.routing_key)


#     async def on_control_message(self, message: AbstractIncomingMessage):
#         logging.info("On Control Message")
#         async with message.process():
#             body, headers = message.body, message.headers
#             body = json.loads( body )
#             if 'fps' in body:
#                 self.fps = int(body['fps'])
#                 self.spf = 1 / self.fps

#         logging.info("Off Control Message")

#             # the example didn't ack back if process() method is used
#             # await message.ask()

#     async def publish(self,):
#         prev_current_time = time.time()
#         while True:
#             current_time = time.time()
#             if current_time - prev_current_time > self.spf:
#                 img, img_header = self.camera.get_frame() # this get the latest frame from the peek queue
#                 body, headers = encode_img(img, img_header)

#                 await self.callback_exchange.publish(
#                     Message(
#                         body = body,
#                         headers=headers,
#                     ),
#                     routing_key=self.publish_routing_key
#                 )
#                 print("Send Live Image ")
#             else:
#                 await asyncio.sleep( self.spf / 10)
#             prev_current_time = current_time



#     async def start(self):
#         await self.create_queue()
#         logging.info(" [x] Start Live Image Message Queue")
#         await self.control_queue.consume(self.on_control_message, no_ack=False)
#         self._publish_task = asyncio.create_task( self.publish() )

class VideoFragmentsMessageQueueServer(BasicServer):
    video_compressor : VideoCompressor

    def __init__(self, video_compressor:VideoCompressor, channel:AbstractChannel, exchange:AbstractExchange, control_routing_key:str, routing_key:str, server_name:str):
        super().__init__(channel=channel, exchange=exchange, control_routing_key=control_routing_key, routing_key=routing_key, server_name=server_name)
        self.video_compressor = video_compressor

    async def on_message(self, message):
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


        body, headers = self.video_compressor.get_history_fragment(fragment_idx)
        headers.update({
                    "size" : num_fragments,
                    "index" : fragment_idx,
                    })
        logging.info(f"fragment length {len(body)}")

        return body, headers

class VideoFragmentsMessageQueueClient(BasicClient):

    async def get(self, n: int, is_initial:bool) -> int:
        logging.info(f"{self.client_name} get fragment {n} is_initial:{is_initial}")
        if is_initial:
            headers = {
                "type":"initial"
            }
        else:
            headers = {
                "type":"history"
            }
        await super().get(message=str(n).encode(), headers=headers)

    async def get_initial(self,) -> int:
        logging.info(f"{self.client_name} call get initial")

        initial_fragments = None
        initial_fragment, initial_fragment_header = await self.get(0, is_initial=True) # get the first frame
        logging.info(f"{self.client_name} get first fragment")

        size = initial_fragment_header['size']
        initial_fragments = [None] * size
        initial_fragments[0] = initial_fragment

        other_fragments_result = await asyncio.gather( *[ self.get(i, is_initial=True) for i in range(1, size)] )
        logging.info(f"{self.client_name} get rest of the fragments with total length {size}")
        for other_fragment_result in other_fragments_result:
            fragment, headers = other_fragment_result
            initial_fragments[ headers["index"] ] = fragment
        logging.info(f"{self.client_name} get rest of the fragments with actual total length {len(initial_fragments)}")

        logging.info(f"{self.client_name} return get initial")

        return initial_fragments

# class VideoFragmentsMessageQueue:
#     video_compressor : VideoCompressor
#     channel : AbstractChannel
#     exchange : AbstractExchange
#     routing_key : str
#     queue : AbstractQueue

#     def __init__(self, video_compressor:VideoCompressor, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str):
#         super().__init__()
#         self.channel = channel
#         self.exchange = exchange
#         self.callback_exchange = self.channel.default_exchange
#         self.routing_key = routing_key
#         self.video_compressor = video_compressor
#         self.queue = None

#     async def create_queue(self):
#         logging.info(" [x] Create temporary queue")

#         self.queue = await self.channel.declare_queue(exclusive=True)        
#         await self.queue.bind(exchange=self.exchange, routing_key=self.routing_key)

#     async def on_message(self, message: AbstractIncomingMessage):
#         async with message.process():
#             assert message.reply_to is not None

#             body = message.body.decode()
#             headers = message.headers
#             # print(body, headers)

#             logging.info(f'Video Initial Queue receive message: {body}')
#             fragment_idx = int(body)
#             logging.info(f"get fragment {fragment_idx}")

#             if headers["type"] == "initial":
#                 num_fragments = self.video_compressor.number_of_startup_fragments() # this get the latest frame from the peek queue
#             else:
#                 num_fragments = 1


#             fragment, headers = self.video_compressor.get_history_fragment(fragment_idx)
#             logging.info(f"fragment length {len(fragment)}")
#             await self.callback_exchange.publish(
#                 Message(
#                     body=fragment,
#                     headers= {
#                         "size" : num_fragments,
#                         "index" : fragment_idx,
#                         **headers
#                     },
#                     correlation_id=message.correlation_id
#                 ),
#                 routing_key=message.reply_to
#             )
#             # print("Message send!")

#     async def start(self):
#         await self.create_queue()

#         logging.info(" [x] Awaiting Video Fragment Request")
#         self._consume_tage = await self.queue.consume(self.on_message, no_ack=False)
#         # async with self.queue.iterator() as qiterator:
#         #     message : AbstractIncomingMessage
#         #     async for message in qiterator:
#         #         try:
#         #             await self.on_message(message=message)
#         #         except Exception:
#         #                 logging.exception("Processing error for message %r", message)

class LiveVideoFragmentsMessageQueueServer(BasicStreamServer):
    video_compressor : VideoCompressor

    def __init__(self, video_compressor:VideoCompressor, channel:AbstractChannel, exchange:AbstractExchange, control_routing_key:str, routing_key:str, publish_routing_key:str, server_name:str):
        super().__init__(
            channel=channel, 
            exchange=exchange, 
            control_routing_key=control_routing_key, 
            routing_key=routing_key, 
            publish_routing_key=publish_routing_key, 
            server_name=server_name
        )
        self.video_compressor = video_compressor

    async def on_message(self):
        fragment, headers = self.video_compressor.get_fragment()
        return fragment, headers

# class VideoMessageQueue:
#     video_compressor : VideoCompressor
#     channel : AbstractChannel
#     exchange : AbstractExchange
#     routing_key : str
#     publish_routing_key : str
#     queue : AbstractQueue

#     def __init__(self, video_compressor:VideoCompressor, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, publish_routing_key:str):
#         super().__init__()
#         self.channel = channel
#         self.exchange = exchange
#         self.routing_key = routing_key
#         self.publish_routing_key = publish_routing_key
#         self.video_compressor = video_compressor
    
#     async def create_queue(self):        
#         self.queue = self.channel.declare_queue(exclusive=True)
#         await self.queue.bind(exchange=self.exchange, routing_key=self.routing_key)

#     async def publish(self):

#         while True:
#             fragment, headers = self.video_compressor.get_fragment()
#             await self.exchange.publish(
#                 message=Message(body=fragment, headers=headers),
#                 routing_key= self.publish_routing_key
#             )
#             # logging.info(f"publish fragment {headers['frag_idx']} frame start from {headers['frame_start']} to {headers['frame_end']}")

#     async def start(self):
#         logging.info(" [x] Start Video Stream")
#         self._publish_task = asyncio.create_task( self.publish() )
#         logging.info(" [x] Start Video Stream end")        
#             # await asyncio.sleep(0.0001)

class LiveVideoFragmentsMessageQueueClient(BasicStreamClient):
    def __init__(self, channel: AbstractChannel, exchange: AbstractExchange, routing_key: str, control_routing_key: str, on_response_callback: Callable[..., Any], client_name: str, time_out: float) -> None:
        super().__init__(channel, exchange, routing_key, control_routing_key, on_response_callback, client_name, time_out)



class VideoRecorderMessageQueue:
    video_recorder : VideoRecorder
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    queue : AbstractQueue

    def __init__(self, video_recorder:VideoRecorder, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str):
        super().__init__()
        self.channel = channel
        self.exchange = exchange
        self.callback_exchange = self.channel.default_exchange
        self.routing_key = routing_key
        self.video_recorder = video_recorder
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

            if body == "start":
                filename = headers["filename"]
                self.video_recorder.start_recording(filename=filename)
            elif body == "end":
                self.video_recorder.end_recording()                
            else:
                raise ValueError(f"Unexpected command '{body}'.")


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
