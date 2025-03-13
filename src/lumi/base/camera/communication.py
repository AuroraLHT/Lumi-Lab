# import pika
# import pika.channel

# Importing the PIL library

import asyncio
import base64
from aio_pika import Message, Channel, Exchange
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractConnection,
    AbstractIncomingMessage,
    AbstractQueue,
)

import numpy as np
import datetime
import struct
import time
import argparse
import logging

import json

from ...utils.image import encode_img
from ...utils.common import encode_json, decode_json

from ..message_queue import (
    BasicServer,
    BasicClient,
    BasicStreamServer,
    BasicStreamClient,
    BaseMessageQueueMessage,
)

from .video_stream import VideoCompressor, VideoRecorder
from .pylon_camera import PylonCamera
from .web_camera import WebCamera
from .test_camera import TestCamera

import queue
from ..models import BaseMessageHeader, BaseControlRequestMessageHeader, BaseRequestMessageHeader, RequestMessageQueueMessage, ResponseMessageQueueMessage, StreamMessageQueueMessage

from typing import Tuple, Union, List, Dict, Any, Callable, Awaitable


class CameraMessageQueueServer(BasicServer):
    camera: Union[
        "PylonCamera", "TestCamera"
    ]

    def __init__(
        self,
        camera,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        request_routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        super().__init__(
            channel=channel,
            exchange=exchange,
            control_routing_key=control_routing_key,
            request_routing_key=request_routing_key,
            state_routing_key=state_routing_key,
            server_name=server_name,
        )
        self.camera = camera

    def update_state(self):
        self.state.update(
            {
                "frame_dims": self.camera.frame_dims,
                "frame_metas": self.camera.frame_metas,
            }
        )

    async def on_message(self, message: AbstractIncomingMessage) -> ResponseMessageQueueMessage:
        body, headers = message.body, message.headers
        
        if headers["request_type"] == "image":
            img, img_header = (
                self.camera.get_frame()
            )  # this get the latest frame from the peek queue

            if img is not None:
                body, headers = encode_img(img, img_header)
                # update the state at each read out

                response= self.create_response_message(
                    body=body,
                    headers=headers,
                    request_type="image",
                    response_type="image",
                    succ=True,
                    error_type="",
                    error_message="",
                )
        elif headers["request_type"] == "get_config":
            response = self.create_response_message(
                body=encode_json(self.camera.get_camera_config()),
                headers={},
                request_type="get_config",
                response_type="get_config",
            )

        elif headers["request_type"] == "update_config":
            config = decode_json(body)
            succ, err_msg = self.camera.update_camera_config(**config)
            logging.info(f"Camera config updated to {config}")
            response = self.create_response_message(
                body=encode_json(self.camera.get_camera_config()),
                headers={},
                request_type="update_config",
                response_type="update_config",
                error_type="UpdateError" if not succ else "",
                error_message=err_msg,
                succ=succ
            )
        else:
            response = self.create_response_message(
                body="".encode(),
                headers={},
                request_type="image",
                response_type="image",
                succ=False,
                error_type="CameraError",
                error_message="No frame available",
            )
        return response


class CameraMessageQueueClient(BasicClient):
    async def get_live_image(self):
        logging.info(f"{self.client_name} get live image")

        request_message = self.create_request_message(
            body="".encode(), headers={}, request_type="image")
        return await self.request(request_message)
    
    async def get_camera_config(self):
        logging.info(f"{self.client_name} get camera config")

        request_message = self.create_request_message(
            body="".encode(), headers={}, request_type="get_config")
        return await self.request(request_message)
    
    async def update_camera_config(self, config: dict):
        logging.info(f"{self.client_name} update camera config")

        request_message = self.create_request_message(
            body=encode_json(config), headers={}, request_type="update_config")
        return await self.request(request_message)


class LiveCameraMessageQueueServer(BasicStreamServer):
    camera: Union[
        "PylonCamera", "WebCamera"
    ]
    camera_queue: queue.Queue

    def __init__(
        self,
        camera,
        camera_queue,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        control_routing_key: str,
        publish_routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        """
        has no input, routing_key could be None
        """
        super().__init__(
            channel=channel,
            exchange=exchange,
            control_routing_key=control_routing_key,
            publish_routing_key=publish_routing_key,
            state_routing_key=state_routing_key,
            server_name=server_name,
        )

        self.camera = camera
        self.camera_queue = camera_queue

    def update_state(self):
        self.state.update(
            {
                "frame_dims": self.camera.frame_dims,
                "frame_metas": self.camera.frame_metas,
            }
        )

    async def on_streaming(self) -> StreamMessageQueueMessage | None:
        if not self.camera_queue.empty():
            img, img_header = self.camera_queue.get()
            body, headers = encode_img(img, img_header)

            response = self.create_stream_message(
                body=body, 
                headers=headers,
                stream_type="live_camera",
            )
        else:
            response = None

        return response


class LiveCameraMessageQueueClient(BasicStreamClient):
    pass


class VideoFragmentsMessageQueueServer(BasicServer):
    video_compressor: "VideoCompressor"

    def __init__(
        self,
        video_compressor: "VideoCompressor",
        channel: AbstractChannel,
        exchange: AbstractExchange,
        control_routing_key: str,
        request_routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        super().__init__(
            channel=channel,
            exchange=exchange,
            control_routing_key=control_routing_key,
            request_routing_key=request_routing_key,
            state_routing_key=state_routing_key,
            server_name=server_name,
        )
        self.video_compressor = video_compressor

    def update_state(self):
        # update the state at each read out
        self.state.update(
            {
                "frame_dims": self.video_compressor.camera.frame_dims,
                "frame_metas": self.video_compressor.camera.frame_metas,
                "video_fps": self.video_compressor.config.fps,
                "video_height": self.video_compressor.config.height,
                "video_width": self.video_compressor.config.width,
            }
        )

    async def on_message(self, message) -> ResponseMessageQueueMessage:
        body = message.body.decode()
        headers : BaseRequestMessageHeader = message.headers

        # logging.info(f"Video Initial Queue receive message: {body}")

        if headers["request_type"] == "initial_fragments_size":
            num_fragments = (
                self.video_compressor.number_of_startup_fragments()
            )  # this get the latest frame from the peek queue
            body = {"size": num_fragments}
            response = self.create_response_message(
                body=encode_json(body), 
                headers={},
                request_type="initial_fragments_size",
                response_type="initial_fragments_size",
                succ=True,
                error_type="",
                error_message="",
            )

        elif headers["request_type"] == "video_fragment":
            fragment_idx = int(body)
            num_fragments = 1

            body, headers = self.video_compressor.get_history_fragment(fragment_idx)
            headers.update({"index": fragment_idx,})

            response = self.create_response_message(
                body=body, 
                headers=headers,
                request_type="video_fragment",
                response_type="video_fragment",
                succ=True,
                error_type="",
                error_message="",
            )

        elif headers["request_type"] == "initial_fragments":
            content = self.video_compressor.get_startup_fragments()
            logging.info(f"fragment length {len(content)}")
            body = {}
            headers = {}
            for idx, (fragment, fragment_headers) in enumerate(content):
                body[str(idx)] = base64.b64encode(fragment).decode('utf-8')
                headers[str(idx)] = fragment_headers

            response = self.create_response_message(
                body=encode_json(body),
                headers=headers,
                request_type="initial_fragments",
                response_type="initial_fragments",
                succ=True,
                error_type="",
                error_message="",
            )

        

        return response

class VideoFragmentsMessageQueueClient(BasicClient):
    
    async def get_initial_fragments_size(self) -> ResponseMessageQueueMessage:
        request_message = self.create_request_message(
            body="".encode(), headers={}, request_type="initial_fragments_size")
        return await self.request(request_message)
    
    async def get_video_fragment(self, fragment_idx: int) -> ResponseMessageQueueMessage:
        request_message = self.create_request_message(
            body=str(fragment_idx).encode(), headers={}, request_type="video_fragment")
        return await self.request(request_message)
    
    async def get_initial_fragments(self) -> ResponseMessageQueueMessage:
        request_message = self.create_request_message(
            body="".encode(), headers={}, request_type="initial_fragments")
        return await self.request(request_message)

    async def get_initial(
        self,
    ) -> List[Tuple[bytes, dict]]:
        logging.info(f"{self.client_name} call get initial")

        initial_fragments : List[Tuple[bytes, dict]] = []
        response = await self.get_initial_fragments_size()  # get the first frame

        logging.info(f"{self.client_name} get first fragment")

        size = decode_json(response.body)["size"]
        initial_fragments = [None] * size

        fragments_result = await asyncio.gather(
            *[self.request(i, is_initial=True) for i in range(0, size)]
        )
        logging.info(
            f"{self.client_name} get rest of the fragments with total length {size}"
        )
        for fragment_result in fragments_result:
            fragment_result : ResponseMessageQueueMessage 
            
            fragment, headers = (
                fragment_result.body,
                fragment_result.headers,
            )
            initial_fragments[headers["index"]] = (fragment, headers)
        logging.info(
            f"{self.client_name} get rest of the fragments with actual total length {len(initial_fragments)}"
        )

        logging.info(f"{self.client_name} return get initial")
        return initial_fragments


class LiveVideoFragmentsMessageQueueServer(BasicStreamServer):
    video_compressor: "VideoCompressor"

    def __init__(
        self,
        video_compressor: "VideoCompressor",
        channel: AbstractChannel,
        exchange: AbstractExchange,
        control_routing_key: str,
        publish_routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        super().__init__(
            channel=channel,
            exchange=exchange,
            control_routing_key=control_routing_key,
            publish_routing_key=publish_routing_key,
            state_routing_key=state_routing_key,
            server_name=server_name,
        )
        self.video_compressor = video_compressor

    def update_state(self):
        # update the state at each read out
        self.state.update(
            {
                "frame_dims": self.video_compressor.camera.frame_dims,
                "frame_metas": self.video_compressor.camera.frame_metas,
                "video_fps": self.video_compressor.config.fps,
                "video_height": self.video_compressor.config.height,
                "video_width": self.video_compressor.config.width,
            }
        )

    async def on_streaming(self) -> StreamMessageQueueMessage | None:
        logging.debug(f"Message queue {self.server_name} server on stream")
        content = self.video_compressor.get_fragment()
        if content is not None:
            fragment, headers = content
            response = self.create_stream_message(
                body=fragment, 
                headers=headers,
                stream_type="live_video",
            )
        else:
            response = None

        return response


class LiveVideoFragmentsMessageQueueClient(BasicStreamClient):
    pass
