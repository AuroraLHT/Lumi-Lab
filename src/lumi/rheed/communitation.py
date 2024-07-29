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

import json

from ..utils.image import encode_img
from ..base.message_queue import BasicServer, BasicStreamServer, BasicStreamClient, BasicClient, MessageQueueResponse, BaseControlMixin

# import lumi
# from .video_stream import VideoCompressor, VideoRecorder
# from .pylon_camera import PylonCamera
# from .web_camera import WebCamera

import queue

from typing import Union, List, Dict, Any, Callable, Awaitable


class CameraMessageQueueServer(BasicServer):
    camera : Union["lumi.rheed.pylon_camera.PylonCamera", "lumi.rheed.pylon_camera.PylonCamera"]

    def __init__(self, camera, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, control_routing_key:str, server_name:str):
        super().__init__(channel=channel, exchange=exchange, control_routing_key=control_routing_key, routing_key=routing_key, server_name=server_name)
        self.camera = camera

    async def on_message(self, message: AbstractIncomingMessage):
        img, img_header = self.camera.get_frame() # this get the latest frame from the peek queue
        body, headers = encode_img(img, img_header)

        return body, headers
    
    @BaseControlMixin.register_control_callback("status")
    async def server_status(self, body, headers):
        """
            TODO: read all the camera adjustable configuation
        """
        status = {
            "frame_dims" : self.camera.frame_dims,
            "frame_metas" : self.camera.frame_metas,
        }
        return MessageQueueResponse(status, {"type":"status"})

class CameraMessageQueueClient(BasicClient):
    async def request(self):
        logging.info(f"{self.client_name} get live image")

        headers = {
            "type":"image"
        }

        return await super().request(body=''.encode(), headers=headers)


class LiveCameraMessageQueueServer(BasicStreamServer):
    camera : Union["lumi.rheed.pylon_camera.PylonCamera", "lumi.rheed.pylon_camera.WebCamera"]
    camera_queue : queue.Queue

    def __init__(
            self, 
            camera,
            camera_queue,
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
        self.camera_queue = camera_queue

    async def on_streaming(self):
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
            on_response_callback: Callable[[AbstractIncomingMessage], Awaitable[bool] ], 
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


class VideoFragmentsMessageQueueServer(BasicServer):
    video_compressor : "lumi.rheed.pylon_camera.VideoCompressor"

    def __init__(self, video_compressor:"lumi.rheed.pylon_camera.VideoCompressor", channel:AbstractChannel, exchange:AbstractExchange, control_routing_key:str, routing_key:str, server_name:str):
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

    async def request(self, n: int, is_initial:bool) -> MessageQueueResponse:
        logging.info(f"{self.client_name} get fragment {n} is_initial:{is_initial}")
        if is_initial:
            headers = {
                "type":"initial"
            }
        else:
            headers = {
                "type":"history"
            }
        return await super().request(body=str(n).encode(), headers=headers)

    async def get_initial(self,) -> List[bytes]:
        logging.info(f"{self.client_name} call get initial")

        initial_fragments = None
        response = await self.request(0, is_initial=True) # get the first frame
        initial_fragment, initial_fragment_header = response.body, response.headers

        logging.info(f"{self.client_name} get first fragment")

        size = initial_fragment_header['size']
        initial_fragments = [None] * size
        initial_fragments[0] = initial_fragment

        other_fragments_result = await asyncio.gather( *[ self.request(i, is_initial=True) for i in range(1, size)] )
        logging.info(f"{self.client_name} get rest of the fragments with total length {size}")
        for other_fragment_result in other_fragments_result:
            fragment, headers = other_fragment_result.body, other_fragment_result.headers
            initial_fragments[ headers["index"] ] = fragment
        logging.info(f"{self.client_name} get rest of the fragments with actual total length {len(initial_fragments)}")

        logging.info(f"{self.client_name} return get initial")

        return initial_fragments


class LiveVideoFragmentsMessageQueueServer(BasicStreamServer):
    video_compressor : "lumi.rheed.pylon_camera.VideoCompressor"

    def __init__(self, video_compressor:"lumi.rheed.pylon_camera.VideoCompressor", channel:AbstractChannel, exchange:AbstractExchange, control_routing_key:str, publish_routing_key:str, server_name:str):
        super().__init__(
            channel=channel, 
            exchange=exchange, 
            control_routing_key=control_routing_key, 
            publish_routing_key=publish_routing_key, 
            server_name=server_name
        )
        self.video_compressor = video_compressor

    async def on_streaming(self):
        logging.debug(f"Message queue {self.server_name} server on stream")
        fragment, headers = self.video_compressor.get_fragment()
        return fragment, headers


class LiveVideoFragmentsMessageQueueClient(BasicStreamClient):
    def __init__(self, channel: AbstractChannel, exchange: AbstractExchange, routing_key: str, control_routing_key: str, on_response_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]], client_name: str, time_out: float) -> None:
        super().__init__(channel, exchange, routing_key, control_routing_key, on_response_callback, client_name, time_out)



class VideoRecorderMessageQueue:
    video_recorder : "lumi.rheed.pylon_camera.VideoRecorder"
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    queue : AbstractQueue

    def __init__(self, video_recorder:"lumi.rheed.pylon_camera.VideoRecorder", channel:AbstractChannel, exchange:AbstractExchange, routing_key:str):
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
