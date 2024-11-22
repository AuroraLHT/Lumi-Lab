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

from ..utils.image import encode_img
from ..utils.common import encode_json, decode_json

from ..base.message_queue import (
    BasicServer,
    BasicClient,
    BasicStreamServer,
    BasicStreamClient,
    BaseMessageQueueMessage,
)

# import lumi
# from .video_stream import VideoCompressor, VideoRecorder
# from .pylon_camera import PylonCamera
# from .web_camera import WebCamera

import queue
from lumi.base.models import BaseMessageHeader, BaseControlRequestMessageHeader, BaseRequestMessageHeader, RequestMessageQueueMessage, ResponseMessageQueueMessage, StreamMessageQueueMessage

from typing import Tuple, Union, List, Dict, Any, Callable, Awaitable


class CameraMessageQueueServer(BasicServer):
    camera: Union[
        "lumi.rheed.pylon_camera.PylonCamera", "lumi.rheed.test_camera.TestCamera"
    ]

    def __init__(
        self,
        camera,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        super().__init__(
            channel=channel,
            exchange=exchange,
            control_routing_key=control_routing_key,
            routing_key=routing_key,
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


class LiveCameraMessageQueueServer(BasicStreamServer):
    camera: Union[
        "lumi.rheed.pylon_camera.PylonCamera", "lumi.rheed.pylon_camera.WebCamera"
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
    def __init__(
        self,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        on_response_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]],
        on_state_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]],
        client_name: str,
        time_out: float,
    ) -> None:

        super().__init__(
            channel=channel,
            exchange=exchange,
            routing_key=routing_key,
            control_routing_key=control_routing_key,
            state_routing_key=state_routing_key,
            on_response_callback=on_response_callback,
            on_state_callback=on_state_callback,
            client_name=client_name,
            time_out=time_out,
        )


class VideoFragmentsMessageQueueServer(BasicServer):
    video_compressor: "lumi.rheed.pylon_camera.VideoCompressor"

    def __init__(
        self,
        video_compressor: "lumi.rheed.pylon_camera.VideoCompressor",
        channel: AbstractChannel,
        exchange: AbstractExchange,
        control_routing_key: str,
        routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        super().__init__(
            channel=channel,
            exchange=exchange,
            control_routing_key=control_routing_key,
            routing_key=routing_key,
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
    video_compressor: "lumi.rheed.video_stream.VideoCompressor"

    def __init__(
        self,
        video_compressor: "lumi.rheed.video_stream.VideoCompressor",
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
    def __init__(
        self,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        on_response_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]],
        on_state_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]],
        client_name: str,
        time_out: float,
    ) -> None:

        super().__init__(
            channel=channel,
            exchange=exchange,
            routing_key=routing_key,
            control_routing_key=control_routing_key,
            state_routing_key=state_routing_key,
            on_response_callback=on_response_callback,
            on_state_callback=on_state_callback,
            client_name=client_name,
            time_out=time_out,
        )


class IntegratorMessageQueueServer(BasicServer):
    integrator: Union[
        "lumi.rheed.integrator.MultiBoxIntegrator"
    ]

    def __init__(
        self,
        integrator: "lumi.rheed.integrator.MultiBoxIntegrator",
        channel: AbstractChannel,
        exchange: AbstractExchange,
        routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        super().__init__(
            channel=channel,
            exchange=exchange,
            control_routing_key=control_routing_key,
            routing_key=routing_key,
            state_routing_key=state_routing_key,
            server_name=server_name,
        )
        self.integrator = integrator

    def update_state(self):
        pass
        # self.state.update(
        #     {
        #         "frame_dims": self.camera.frame_dims,
        #         "frame_metas": self.camera.frame_metas,
        #     }
        # )

    async def on_message(self, message: AbstractIncomingMessage) -> RequestMessageQueueMessage:
        message_headers = message.headers
        message_body = decode_json(message.body)

        if message_headers["request_type"] == "cache":
            logging.info(f"{self.log_prefix} get cache for bbox_id {message_body['bbox_id']}")
            bbox_id = message_body["bbox_id"]
            contents = self.integrator.get_integration_cache(bbox_id)
            body = encode_json(contents)
            headers = {}
            response = self.create_response_message(
                body=body,
                headers=headers,
                request_type="cache",
                response_type="cache",
                succ=True,
                error_type="",
                error_message="",
            )

        elif message_headers["request_type"] == "register":
            logging.info(f"{self.log_prefix} register integration for bbox_id {message_body['bbox_id']}")
            bbox_id = message_body["bbox_id"]
            bbox = message_body["bbox"]
            self.integrator.register_bbox(bbox=bbox, bbox_id=bbox_id)
            body = "".encode()
            headers = {}
            response = self.create_response_message(
                body=body,
                headers=headers,
                request_type="register",
                response_type="register",
                succ=True,
                error_type="",
                error_message="",
            )

        elif message_headers["request_type"] == "remove":
            logging.info(f"{self.log_prefix} remove integration for bbox_id {message_body['bbox_id']}")
            bbox_id = message_body["bbox_id"]
            self.integrator.remove_bbox(bbox_id)
            body = "".encode()
            headers = {}
            response = self.create_response_message(
                body=body,
                headers=headers,
                request_type="remove",
                response_type="remove",
                succ=True,
                error_type="",
                error_message="",
            )

        elif message_headers["request_type"] == "bboxes":
            logging.info(f"{self.log_prefix} get bboxes")
            bboxes = self.integrator.bboxes
            body = encode_json(bboxes)
            headers = {}
            response = self.create_response_message(
                body=body,
                headers=headers,
                request_type="bboxes",
                response_type="bboxes",
                succ=True,
                error_type="",
                error_message="",
            )

        else:
            # raise ValueError(f"Unexpected message type: {message_headers['type']}")
            response = self.create_response_message(
                body=b"",
                headers={},
                request_type="bboxes",
                response_type="bboxes",
                succ=False,
                error_type="UnknownRequest",
                error_message=f"Unexpected message type: {message_headers['request_type']}",
            )

        return response


class IntegratorMessageQueueClient(BasicClient):
    
    async def get_cache(self, bbox_id: int):
        logging.info(f"{self.log_prefix} get cache for bbox_id {bbox_id}")
        body = encode_json({"bbox_id": bbox_id})
        headers = {}
        request_message = self.create_request_message(
            body=body, headers=headers, request_type="cache")
        return await self.request(request_message)
    
    async def get_bboxes(self):
        logging.info(f"{self.log_prefix} get bboxes")
        request_message = self.create_request_message(
            body="".encode(), headers={}, request_type="bboxes")
        return await self.request(request_message)
    
    async def register_bbox(self, bbox_id: int, bbox: Dict[str, Any]):
        logging.info(f"{self.log_prefix} register bbox {bbox_id}")
        body = encode_json({"bbox_id": bbox_id, "bbox": bbox})
        headers = {}
        request_message = self.create_request_message(
            body=body, headers=headers, request_type="register")
        return await self.request(request_message)

    async def remove_bbox(self, bbox_id: int):
        logging.info(f"{self.log_prefix} remove bbox {bbox_id}")
        body = encode_json({"bbox_id": bbox_id})
        headers = {}
        request_message = self.create_request_message(
            body=body, headers=headers, request_type="remove")
        return await self.request(request_message)


class LiveIntegratorMessageQueueServer(BasicStreamServer):
    integrator: Union[
        "lumi.rheed.pylon_camera.PylonCamera", "lumi.rheed.pylon_camera.WebCamera"
    ]
    integrator_queue: queue.Queue

    def __init__(
        self,
        integrator,
        integrator_queue,
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

        self.integrator = integrator
        self.integrator_queue = integrator_queue

    def update_state(self):
        pass
        # self.state.update(
        #     {
        #         "frame_dims": self.camera.frame_dims,
        #         "frame_metas": self.camera.frame_metas,
        #     }
        # )

    async def on_streaming(self) -> StreamMessageQueueMessage | None:
        if not self.integrator_queue.empty():
            integration, integration_header = self.integrator_queue.get()
            try:
                body, headers = encode_json(integration), integration_header
                # logging.info(f"Integrator Message Queue Server on stream: {body}")
                response = self.create_stream_message(
                    body=body, 
                    headers=headers,
                    stream_type="live_integrator",
                )
            except Exception as e:
                logging.error(f"Integrator Message Queue Server on stream: {e}")
                response = None
        else:
            response = None

        return response


class LiveIntegratorMessageQueueClient(BasicStreamClient):
    def __init__(
        self,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        on_response_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]],
        on_state_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]],
        client_name: str,
        time_out: float,
    ) -> None:

        super().__init__(
            channel=channel,
            exchange=exchange,
            routing_key=routing_key,
            control_routing_key=control_routing_key,
            state_routing_key=state_routing_key,
            on_response_callback=on_response_callback,
            on_state_callback=on_state_callback,
            client_name=client_name,
            time_out=time_out,
        )


class STFTMessageQueueServer(BasicServer):
    stft_calculator: "lumi.rheed.livefft.STFTCalculator"
    

    def __init__(
        self,
        stft_calculator: "lumi.rheed.livefft.STFTCalculator",
        channel: AbstractChannel,
        exchange: AbstractExchange,
        routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        server_name: str,
    ):
        super().__init__(
            channel=channel,
            exchange=exchange,
            control_routing_key=control_routing_key,
            routing_key=routing_key,
            state_routing_key=state_routing_key,
            server_name=server_name,
        )
        self.stft_calculator = stft_calculator

    def update_state(self):
        pass
        # self.state.update(
        #     {
        #         "frame_dims": self.camera.frame_dims,
        #         "frame_metas": self.camera.frame_metas,
        #     }
        # )

    async def on_message(self, message: AbstractIncomingMessage) -> RequestMessageQueueMessage:
        message_headers = message.headers

        if message_headers["request_type"] == "cache":
            message_body = decode_json(message.body)
            bbox_id = message_body["bbox_id"]
            contents = self.stft_calculator.get_cache(bbox_id)
            body = encode_json(contents)
            headers = {}
            
            logging.info(f"{self.log_prefix} get cache for bbox_id {message_body['bbox_id']}")
            response = self.create_response_message(
                body=body,
                headers=headers,
                request_type="cache",
                response_type="cache",
                succ=True,
                error_type="",
                error_message="",
            )

        elif message_headers["request_type"] == "register":
            message_body = decode_json(message.body)
            bbox_id = message_body["bbox_id"]
            self.stft_calculator.register_integration(bbox_id)
            body = "".encode()
            headers = {}

            logging.info(f"{self.log_prefix} register integration for bbox_id {message_body['bbox_id']}")
            response = self.create_response_message(
                body=body,
                headers=headers,
                request_type="register",
                response_type="register",
                succ=True,
                error_type="",
                error_message="",
            )
        
        elif message_headers["request_type"] == "remove":
            message_body = decode_json(message.body)
            bbox_id = message_body["bbox_id"]
            self.stft_calculator.remove_integration(bbox_id)
            body = "".encode()
            headers = {}

            logging.info(f"{self.log_prefix} remove integration for bbox_id {message_body['bbox_id']}")
            response = self.create_response_message(
                body=body,
                headers=headers,
                request_type="remove",
                response_type="remove",
                succ=True,
                error_type="",
                error_message="",
            )

        elif message_headers["request_type"] == "bboxes":
            bboxes = self.stft_calculator.registered_integrations
            body = encode_json(bboxes)
            headers = {}

            logging.info(f"{self.log_prefix} get bboxes")
            response = self.create_response_message(
                body=body,
                headers=headers,
                request_type="bboxes",
                response_type="bboxes",
                succ=True,
                error_type="",
                error_message="",
            )
        else:
            raise ValueError(f"Unexpected message type: {message_headers['request_type']}")
        
        return response


class STFTMessageQueueClient(BasicClient):
    
    async def get_cache(self, bbox_id: int):
        logging.info(f"{self.log_prefix} get cache for bbox_id {bbox_id}")
        body = encode_json({"bbox_id": bbox_id})
        headers = {}
        request_message = self.create_request_message(
            body=body, headers=headers, request_type="cache")
        return await self.request(request_message)
    
    async def get_bboxes(self):
        logging.info(f"{self.log_prefix} get bboxes")
        headers = {}
        request_message = self.create_request_message(
            body="".encode(), headers=headers, request_type="bboxes")
        return await self.request(request_message)
    
    async def register_bbox(self, bbox_id: int):
        logging.info(f"{self.log_prefix} register bbox {bbox_id}")
        body = encode_json({"bbox_id": bbox_id})
        headers = {}
        request_message = self.create_request_message(
            body=body, headers=headers, request_type="register")
        return await self.request(request_message)
    
    async def remove_bbox(self, bbox_id: int):
        logging.info(f"{self.log_prefix} remove bbox {bbox_id}")
        body = encode_json({"bbox_id": bbox_id})
        headers = {}
        request_message = self.create_request_message(
            body=body, headers=headers, request_type="remove")
        return await self.request(request_message)


class LiveSTFTMessageQueueServer(BasicStreamServer):
    stft_calculator: Union[
        "lumi.rheed.livefft.STFTCalculator"
    ]
    stft_calculator_queue: queue.Queue

    def __init__(
        self,
        stft_calculator,
        stft_calculator_queue,
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

        self.stft_calculator = stft_calculator
        self.stft_calculator_queue = stft_calculator_queue

    def update_state(self):
        pass

    async def on_streaming(self) -> StreamMessageQueueMessage | None:
        if not self.stft_calculator_queue.empty():
            stft_body, stft_header = self.stft_calculator_queue.get()
            body, headers = encode_json(stft_body), stft_header

            response = self.create_stream_message(
                body=body, 
                headers=headers,
                stream_type="live_stft",
                )
        else:
            response = None

        return response


class LiveSTFTMessageQueueClient(BasicStreamClient):
    def __init__(
        self,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
        on_response_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]],
        on_state_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]],
        client_name: str,
        time_out: float,
    ) -> None:

        super().__init__(
            channel=channel,
            exchange=exchange,
            routing_key=routing_key,
            control_routing_key=control_routing_key,
            state_routing_key=state_routing_key,
            on_response_callback=on_response_callback,
            on_state_callback=on_state_callback,
            client_name=client_name,
            time_out=time_out,
        )

# class VideoRecorderMessageQueue:
#     video_recorder: "lumi.rheed.pylon_camera.VideoRecorder"
#     channel: AbstractChannel
#     exchange: AbstractExchange
#     routing_key: str
#     queue: AbstractQueue

#     def __init__(
#         self,
#         video_recorder: "lumi.rheed.pylon_camera.VideoRecorder",
#         channel: AbstractChannel,
#         exchange: AbstractExchange,
#         routing_key: str,
#     ):
#         super().__init__()
#         self.channel = channel
#         self.exchange = exchange
#         self.callback_exchange = self.channel.default_exchange
#         self.routing_key = routing_key
#         self.video_recorder = video_recorder
#         self.queue = None

#     async def create_queue(self):
#         logging.info(" [x] Create temporary queue")

#         self.queue = await self.channel.declare_queue(exclusive=True)
#         await self.queue.bind(exchange=self.exchange, routing_key=self.routing_key)

#     async def on_message(self, message: AbstractIncomingMessage):
#         body = message.body.decode()
#         headers = message.headers
#         # print(body, headers)

#         if body == "start":
#             filename = headers["filename"]
#             self.video_recorder.start_recording(filename=filename)
#         elif body == "end":
#             self.video_recorder.end_recording()
#         else:
#             raise ValueError(f"Unexpected command '{body}'.")

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
