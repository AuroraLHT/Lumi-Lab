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


# class ImageMessageQueueClient(BasicClient):
#     async def get(self):
#         headers = { "type":"image" }
#         message = ''.encode()

#         return await super().get(message=message, headers=headers)

# class ImageMessageQueueClient:
#     channel : AbstractChannel
#     exchange : AbstractExchange
#     routing_key : str
#     queue : AbstractQueue

#     def __init__(self, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str) -> None:
#         self.channel = channel
#         self.exchange = exchange
#         self.routing_key = routing_key
#         self.queue = None

#     async def start(self):
#         self.callback_queue = await self.channel.declare_queue(exclusive=True)
#         self.futures = {}

#         # the consume here is not blocking
#         await self.callback_queue.consume(self.on_response, no_ack=True)
#         print("image client start up")
#         return self

#     async def on_response(self, message: AbstractIncomingMessage) -> None:
#         if message.correlation_id is None:
#             logging.info(f"Bad message {message!r}")
#             return

#         future: asyncio.Future = self.futures.pop(message.correlation_id)
#         future.set_result( (message.body, message.headers) )

#     async def get(self) -> int:
#         print(f"Get live image")
#         correlation_id = str(uuid.uuid4())
#         loop = asyncio.get_running_loop()
#         future = loop.create_future()

#         self.futures[correlation_id] = future
#         headers = {
#             "type":"image"
#         }

#         await self.exchange.publish(
#             Message(
#                 ''.encode(),
#                 content_type="text/plain",
#                 correlation_id=correlation_id,
#                 reply_to=self.callback_queue.name,
#                 headers=headers
#             ),
#             routing_key=self.routing_key,
#         )

#         return await future

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
        detector_output, detector_output_headers = self.detector.predict(img)
        logging.info(f"{self.server_type} <{self.server_name}> detection acquired")

        try:
            body, headers = encode_detections(detector_output=detector_output, detector_output_headers=detector_output_headers)
        except Exception as e:
            print(e)
            raise e
        logging.info("{self.server_type} <{self.server_name}> detection encoded")
        return body, headers

class LiveDetectionMessageQueueClient(BasicStreamClient):
    def __init__(self, channel: AbstractChannel, exchange: AbstractExchange, routing_key: str, control_routing_key: str, on_response_callback: Callable[..., Any], client_name: str, time_out: float) -> None:
        super().__init__(channel, exchange, routing_key, control_routing_key, on_response_callback, client_name, time_out)
#     pass


# class LiveDetectionMessageQueue:

#     detector : DetectorServer
#     image_client: ImageMessageQueueClient
    
#     channel : AbstractChannel
#     exchange : AbstractExchange
#     routing_key : str

#     queue : AbstractQueue
#     control_queue : AbstractQueue

#     start_flag : bool

#     def __init__(self, detector, image_client:ImageMessageQueueClient, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, control_routing_key:str, publish_routing_key:str):
#         """
#             No need input routing key
#         """
#         super().__init__()
#         self.channel = channel
#         self.exchange = exchange

#         self.routing_key = routing_key
#         self.control_routing_key = control_routing_key
#         self.publish_routing_key = publish_routing_key

#         self.detector = detector
#         self.image_client = image_client

#         self.queue = None
#         self.control_queue = None

#         self.start_flag = False

#         self._fps = 0

#     async def create_queue(self):
#         logging.info(" [x] Create temporary queue")
#         # for getting live image
#         # self.queue = await self.channel.declare_queue(exclusive=True)
#         # await self.queue.bind(self.exchange, self.routing_key)

#         # for control
#         self.control_queue = await self.channel.declare_queue(exclusive=True)
#         await self.control_queue.bind(self.exchange, self.control_routing_key)


#     async def publish(self):
#         while True:
#             body, headers = await self.image_client.get()
#             logging.info("Live Image Acquire")
#             img, img_header = decode_img(body, headers)
            
#             if self.start_flag:
#                 #TODO: we could move the whole AI stack into seperate backend API server then this could be awaitable
#                 detector_output = self.detector.predict(img, headers)
#                 logging.info("Detection Acquired")

#                 # print(detector_output)
#                 result = {
#                     "bboxes" : detector_output["bboxes"],
#                     "classification" : detector_output["classification"]
#                 }
#                 # print(result)
                    
#                 body = json.dumps( result ).encode()
#                 # print(body)
#                 time_stamp = img_header['time_stamp'] if 'time_stamp' in img_header else ""
#                 headers = {"time_stamp":time_stamp}

#                 logging.info(f"Publish live detection for {time_stamp}")

#                 await self.exchange.publish(
#                     Message(
#                         body = body,
#                         headers = headers,
#                     ),
#                     routing_key=self.publish_routing_key
#                 )
#                 print("Send AI detection ")
#             else:
#                 print("Start Flag is off")
#                 asyncio.sleep(0.1)

#         # the example didn't ack back if process() method is used
#         # await message.ask()

#     async def on_control_message(self, message : AbstractIncomingMessage):
#         async with message.process():
#             body, headers = message.body, message.headers
#             ctrl = body.decode()
#             logging.info(f"on control message '{ctrl}'.")
            
#             if ctrl == "start":
#                 self.start_flag = True
#             elif ctrl == "stop":
#                 self.start_flag = False
#             else:
#                 logging.info(f"Unknown ctrl text '{ctrl}'")

#     async def start(self):
#         await self.create_queue()
#         logging.info("[x] start live detection message queue")
#         try:
#             # self._consume_tag = await self.queue.consume(callback=self.on_message ,no_ack=True)
#             self._control_consume_tag = await self.control_queue.consume(callback=self.on_control_message ,no_ack=False)
#             self._publish_task = await asyncio.create_task(self.publish())
#         except Exception as e:
#             print(e)

#     async def cancel(self):
#         await self.queue.cancel(self._consume_tag)
#         await self._control_consume_tag.cancel(self._control_consume_tag)




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

        detector_output, detector_output_headers = self.detector.predict(img)
        body, headers = encode_detections(detector_output=detector_output, detector_output_headers=detector_output_headers)

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


# class DetectionMessageQueue:
#     channel : AbstractChannel
#     exchange : AbstractExchange
#     routing_key : str
#     queue : AbstractQueue

#     def __init__(self, detector, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str):
#         super().__init__()
#         self.channel = channel
#         self.exchange = exchange
#         self.callback_exchange = channel.default_exchange
#         self.routing_key = routing_key
#         self.detector = detector
#         self.queue = None

#     async def create_queue(self):
#         logging.info(" [x] Create temporary queue")

#         self.queue = await self.channel.declare_queue(exclusive=True)
#         await self.queue.bind(self.exchange, routing_key=self.routing_key)


#     async def on_message(self, message: AbstractIncomingMessage):
#         logging.info("Detection message queue on request")
#         async with message.process():
#             body, headers = message.body, message.headers
#             img, img_header = decode_img(body, headers)

#             detector_output = self.detector.predict(img)

#             result = {
#                 "bboxes" : detector_output["bboxes"],
#                 "classification" : detector_output["classification"]
#             }
                
#             body = json.dumps( result )
#             headers = {}

#             await self.callback_exchange.publish(
#                 Message(
#                     body = body,
#                     correlation_id = message.correlation_id,
#                     headers = headers,
#                 ),
#                 routing_key=message.reply_to
#             )
#         logging.info("Send AI detection ")

#             # the example didn't ack back if process() method is used
#             # await message.ask()


#     async def start(self):
#         await self.create_queue()
#         logging.info("[x] start detection message queue")

#         self._consume_tag = await self.queue.consume(self.on_message, no_ack=False)
#         # async with self.queue.iterator() as qiterator:
#         #     message : AbstractIncomingMessage
#         #     async for message in qiterator:
#         #         try:
#         #             await self.on_message(message=message)
#         #         except Exception:
#         #             logging.exception("Processing error for message %r", message)
