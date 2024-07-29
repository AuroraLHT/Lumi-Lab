import logging

import uuid
import asyncio
import aio_pika

from aio_pika import Message, connect
from aio_pika.abc import (
    AbstractChannel, AbstractConnection, AbstractExchange, AbstractQueue, AbstractIncomingMessage, AbstractQueue,
)

from collections.abc import Callable, Awaitable

import json
# from misc import decode_img
from ..utils.image import decode_img, encode_img, encode_mask, decode_mask

# import lumi
# from .model import DetectorServer
from ..base.message_queue import BasicClient, BasicStreamClient, BasicServer, BasicStreamServer, BaseControlMixin, MessageQueueResponse
from ..rheed.communitation import CameraMessageQueueClient

import copy

from typing import List, Dict, Any, Union

def encode_detections(detector_output, detector_output_headers):
    pattern = detector_output["instance_segementation"].rd.pattern
    img, img_headers = encode_img(pattern, {}, to_base64=True)

    bboxes = copy.deepcopy( detector_output["bboxes"] ) 
    for k, bbox in bboxes.items():
        mask, masks_headers = encode_mask(bbox["mask"], {}, to_base64=True)

        bbox["mask"] = {"mask":mask, "mask_headers":masks_headers}

    result = {
        "pattern" : {"pattern":img, "pattern_headers":img_headers},
        "bboxes" : bboxes,
        "classification" : detector_output["classification"],
        "region2tracks" : detector_output["region2tracks"],
    }
        
    body = json.dumps( result ).encode()

    return body, detector_output_headers

def decode_detections(body, headers):
    detector_output = json.loads(body)
    detector_output_headers = headers

    pattern, pattern_headers = decode_img(detector_output["pattern"]["pattern"], detector_output["pattern"]["pattern_headers"], from_base64=True)
    detector_output["pattern"]["pattern"] = pattern
    detector_output["pattern"]["pattern_headers"] = pattern_headers

    bboxes = detector_output["bboxes"]
    for k, bbox in bboxes.items():
        bbox["mask"]["mask"], bbox["mask"]["mask_headers"] = decode_mask(bbox["mask"]["mask"], bbox["mask"]["mask_headers"], from_base64=True)

    return detector_output, detector_output_headers


# TODO I would like to integrate tracking into this MessageQueue since passing all prediction around the message queue invole compress and decompression
class LiveDetectionMessageQueueServer(BasicStreamServer):
    detector : "lumi.detection.model.DetectorServer"
    image_client: CameraMessageQueueClient
    fps : int

    def __init__(
            self, 
            detector, 
            camera_client:CameraMessageQueueClient, 
            channel:AbstractChannel, 
            exchange:AbstractExchange, 
            control_routing_key:str, 
            publish_routing_key:str, 
            server_name:str
        ):

        """
            No need input routing key
        """
        super().__init__(
            channel = channel, 
            exchange = exchange, 
            control_routing_key = control_routing_key, 
            publish_routing_key = publish_routing_key, 
            server_name = server_name
        )

        self.detector = detector
        self.camera_client = camera_client
        self.fps = 0

    async def on_streaming(self):
        response = await self.camera_client.request()
        img, img_header = decode_img(response.body, response.headers)
            
        #TODO: we could move the whole AI stack into seperate backend API server then this could be awaitable
        detector_output, detector_output_headers = self.detector.predict(img, img_header)
        logging.info(f"{self.server_type} <{self.server_name}> detection acquired")

        try:
            body, headers = encode_detections(detector_output=detector_output, detector_output_headers=detector_output_headers)
        except Exception as e:
            print(e)
            raise e
        logging.info("{self.server_type} <{self.server_name}> detection encoded")
        return body, headers

class LiveDetectionMessageQueueClient(BasicStreamClient):
    def __init__(self, channel: AbstractChannel, exchange: AbstractExchange, routing_key: str, control_routing_key: str, on_response_callback: Callable[[AbstractIncomingMessage], Awaitable[bool]], client_name: str, time_out: float) -> None:
        super().__init__(channel, exchange, routing_key, control_routing_key, on_response_callback, client_name, time_out)


class DetectionMessageQueueServer(BasicServer):
    # detector : DetectorServer
    detector : "lumi.detection.model.DetectorServer"
    camera_client:CameraMessageQueueClient

    def __init__(self, detector, camera_client, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, control_routing_key:str, server_name:str):
        super().__init__(channel=channel, exchange=exchange, control_routing_key=control_routing_key, routing_key=routing_key, server_name=server_name)
        self.detector = detector
        self.camera_client = camera_client

    async def on_message(self, message:AbstractIncomingMessage):
        body, headers = message.body, message.headers
        if len(message.body):
            body, headers = await self.camera_client.request()
        img, img_header = decode_img(body, headers)

        detector_output, detector_output_headers = self.detector.predict(img, img_header)
        body, headers = encode_detections(
            detector_output=detector_output, 
            detector_output_headers=detector_output_headers
        )

        return body, headers
    
    @BaseControlMixin.register_control_callback("status")
    async def server_status(self, body, headers):
        status = {
            "pattern_dim" : self.detector.pattern_dims,
            "detection_metas" : self.detector.metas,
            "classifier_classes" : self.detector.aux_detector.classifier_classes,
        }
        return MessageQueueResponse(status, {"type":"status"})

class DetectionMessageQueueClient(BasicClient):
    def __init__(self, channel: AbstractChannel, exchange: AbstractExchange, routing_key: str, control_routing_key: str, client_name: str, time_out: float) -> None:
        super().__init__(channel, exchange, routing_key, control_routing_key, client_name, time_out)

    async def request(self, image=None, image_headers=None):
        logging.info(f"{self.client_name} get detection")
        image_headers = {} if image_headers is not None else image_headers
        body, headers = encode_img(image, image_headers)
        headers.update( { "type":"detection" } )

        return await super().request( body=body, headers=headers )
