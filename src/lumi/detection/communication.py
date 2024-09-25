import logging

import uuid
import asyncio
import aio_pika

from aio_pika import Message, connect
from aio_pika.abc import (
    AbstractChannel,
    AbstractConnection,
    AbstractExchange,
    AbstractQueue,
    AbstractIncomingMessage,
    AbstractQueue,
)

from collections.abc import Callable, Awaitable

import json

import numpy as np

# from misc import decode_img
from ..utils.image import decode_img, encode_img, encode_mask, decode_mask

# import lumi
# from .model import DetectorServer
from ..base.message_queue import (
    BasicClient,
    BasicStreamClient,
    BasicServer,
    BasicStreamServer,
    BaseControlMixin,
    MessageQueueResponse,
)
from ..rheed.communication import CameraMessageQueueClient

import copy

from typing import List, Dict, Any, Union


def encode_detections(detector_output, detector_output_headers, drop_mask=False, drop_pattern=False):
    pattern = detector_output["instance_segementation"].rd.pattern
    if drop_pattern:
        pattern = None
    else:
        img, img_headers = encode_img(pattern, {}, to_base64=True)
        pattern = {"pattern": img, "pattern_headers": img_headers}

    bboxes = copy.deepcopy(detector_output["bboxes"])

    if drop_mask:
        for k, bbox in bboxes.items():
            bbox["mask"] = None
    else:
        for k, bbox in bboxes.items():
            mask, masks_headers = encode_mask(bbox["mask"], {}, to_base64=True)
            bbox["mask"] = {"mask": mask, "mask_headers": masks_headers}

    result = {
        "pattern": pattern,
        "bboxes": bboxes,
        "classification": detector_output["classification"],
        "region2tracks": detector_output["region2tracks"],
    }

    body = json.dumps(result).encode()

    return body, detector_output_headers


def decode_detections(body, headers):
    detector_output = json.loads(body)
    detector_output_headers = headers
    
    if "pattern" in detector_output and detector_output["pattern"] is not None:
        pattern, pattern_headers = decode_img(
            detector_output["pattern"]["pattern"],
            detector_output["pattern"]["pattern_headers"],
            from_base64=True,
        )
        detector_output["pattern"]["pattern"] = pattern
        detector_output["pattern"]["pattern_headers"] = pattern_headers

    bboxes = detector_output["bboxes"]
    for k, bbox in bboxes.items():
        if bbox["mask"] is not None:
            bbox["mask"]["mask"], bbox["mask"]["mask_headers"] = decode_mask(
                bbox["mask"]["mask"], bbox["mask"]["mask_headers"], from_base64=True
            )

    return detector_output, detector_output_headers


# TODO I would like to integrate tracking into this MessageQueue since passing all prediction around the message queue invole compress and decompression
class LiveDetectionMessageQueueServer(BasicStreamServer):
    detector: "lumi.detection.model.DetectorServer"
    camera_client: CameraMessageQueueClient
    fps: int
    drop_mask: bool
    drop_pattern: bool
    def __init__(
        self,
        detector,
        camera_client: CameraMessageQueueClient,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        control_routing_key: str,
        publish_routing_key: str,
        state_routing_key: str,
        server_name: str,
        drop_mask: bool = False,
        drop_pattern: bool = False,
    ):
        """
        No need input routing key
        """
        super().__init__(
            channel=channel,
            exchange=exchange,
            control_routing_key=control_routing_key,
            publish_routing_key=publish_routing_key,
            state_routing_key=state_routing_key,
            server_name=server_name,
        )

        self.detector = detector
        self.camera_client = camera_client
        self.fps = 0
        self.drop_mask = drop_mask
        self.drop_pattern = drop_pattern
        self.cache = None

    def update_state(self):
        self.state.update(
            {
                "pattern_dim": self.detector.pattern_dims,
                "detection_metas": self.detector.metas,
                "classifier_classes": self.detector.aux_detector.classifier_classes,
            }
        )

    async def on_streaming(self):
        # if self.cache is None:
        # await asyncio.sleep(1) # throttle test
        response = await self.camera_client.request()
                
        if response.body is None:
            logging.warning(f"{self.log_prefix} Received None response from camera")
            return None, None
        
        if self.cache is None:
            img, img_header = decode_img(response.body, response.headers)
            # TODO: we could move the whole AI stack into seperate backend API server then this could be awaitable
            detector_output, detector_output_headers = self.detector.predict(
                img, img_header
            )
            # self.cache = (img, img_header, detector_output, detector_output_headers)
        else:
            await asyncio.sleep(0.2) # 5Hz update speed
            img, img_header, detector_output, detector_output_headers = self.cache
        logging.info(f"{self.log_prefix} detection acquired")

        try:
            body, headers = encode_detections(
                detector_output=detector_output,
                detector_output_headers=detector_output_headers,
                drop_mask=self.drop_mask,
                drop_pattern=self.drop_pattern,
            )
        except Exception as e:
            logging.error(f"{self.log_prefix} Failed to encode detection: {e}")
            raise e
        
        logging.info(f"{self.log_prefix} detection encoded")
        return body, headers


class LiveDetectionMessageQueueClient(BasicStreamClient):
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


class DetectionMessageQueueServer(BasicServer):
    # detector : DetectorServer
    detector: "lumi.detection.model.DetectorServer"
    camera_client: CameraMessageQueueClient

    def __init__(
        self,
        detector,
        camera_client,
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
        self.detector = detector
        self.camera_client = camera_client

    def update_state(self):
        self.state.update(
            {
                "pattern_dim": self.detector.pattern_dims,
                "detection_metas": self.detector.metas,
                "classifier_classes": self.detector.aux_detector.classifier_classes,
            }
        )


    async def on_message(self, message: AbstractIncomingMessage):
        body, headers = message.body, message.headers
        if len(message.body):
            body, headers = await self.camera_client.request()
        img, img_header = decode_img(body, headers)

        detector_output, detector_output_headers = self.detector.predict(
            img, img_header
        )
        body, headers = encode_detections(
            detector_output=detector_output,
            detector_output_headers=detector_output_headers,
        )

        return body, headers


class DetectionMessageQueueClient(BasicClient):
    def __init__(
        self,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        routing_key: str,
        control_routing_key: str,
        state_routing_key: str,
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
            on_state_callback=on_state_callback,
            client_name=client_name,
            time_out=time_out,
        )

    async def request(self, image=None, image_headers=None):
        logging.info(f"{self.client_name} get detection")
        image_headers = {} if image_headers is not None else image_headers
        body, headers = encode_img(image, image_headers)
        headers.update({"type": "detection"})

        return await super().request(body=body, headers=headers)
