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

from misc import decode_img

from model import DetectorServer


class ImageMessageQueueClient:
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    queue : AbstractQueue

    def __init__(self, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str) -> None:
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.queue = None

    async def start(self):
        self.callback_queue = await self.channel.declare_queue(exclusive=True)
        self.futures = {}

        # the consume here is not blocking
        await self.callback_queue.consume(self.on_response, no_ack=True)
        print("image client start up")
        return self

    async def on_response(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id is None:
            logging.info(f"Bad message {message!r}")
            return

        future: asyncio.Future = self.futures.pop(message.correlation_id)
        future.set_result( (message.body, message.headers) )

    async def get(self) -> int:
        print(f"Get live image")
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        self.futures[correlation_id] = future
        headers = {
            "type":"image"
        }

        await self.exchange.publish(
            Message(
                ''.encode(),
                content_type="text/plain",
                correlation_id=correlation_id,
                reply_to=self.callback_queue.name,
                headers=headers
            ),
            routing_key=self.routing_key,
        )

        return await future

# TODO I would like to integrate tracking into this MessageQueue since passing all prediction aroudn the message queue invole compress and decompression
class LiveDetectionMessageQueue:

    detector : DetectorServer
    image_client: ImageMessageQueueClient
    
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str

    queue : AbstractQueue
    control_queue : AbstractQueue

    start_flag : bool

    def __init__(self, detector, image_client:ImageMessageQueueClient, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, control_routing_key:str, publish_routing_key:str):
        """
            No need input routing key
        """
        super().__init__()
        self.channel = channel
        self.exchange = exchange

        self.routing_key = routing_key
        self.control_routing_key = control_routing_key
        self.publish_routing_key = publish_routing_key

        self.detector = detector
        self.image_client = image_client

        self.queue = None
        self.control_queue = None

        self.start_flag = False

        self._fps = 0

    async def create_queue(self):
        logging.info(" [x] Create temporary queue")
        # for getting live image
        # self.queue = await self.channel.declare_queue(exclusive=True)
        # await self.queue.bind(self.exchange, self.routing_key)

        # for control
        self.control_queue = await self.channel.declare_queue(exclusive=True)
        await self.control_queue.bind(self.exchange, self.control_routing_key)


    async def publish(self):
        while True:
            body, headers = await self.image_client.get()
            logging.info("Live Image Acquire")
            img, img_header = decode_img(body, headers)
            
            if self.start_flag:
                #TODO: we could move the whole AI stack into seperate backend API server then this could be awaitable
                detector_output = self.detector.predict(img, headers)
                logging.info("Detection Acquired")

                # print(detector_output)
                result = {
                    "bboxes" : detector_output["bboxes"],
                    "classification" : detector_output["classification"]
                }
                # print(result)
                    
                body = json.dumps( result ).encode()
                # print(body)
                time_stamp = img_header['time_stamp'] if 'time_stamp' in img_header else ""
                headers = {"time_stamp":time_stamp}

                logging.info(f"Publish live detection for {time_stamp}")

                await self.exchange.publish(
                    Message(
                        body = body,
                        headers = headers,
                    ),
                    routing_key=self.publish_routing_key
                )
                print("Send AI detection ")
            else:
                print("Start Flag is off")
                asyncio.sleep(0.1)

        # the example didn't ack back if process() method is used
        # await message.ask()

    async def on_control_message(self, message : AbstractIncomingMessage):
        async with message.process():
            body, headers = message.body, message.headers
            ctrl = body.decode()
            logging.info(f"on control message '{ctrl}'.")
            
            if ctrl == "start":
                self.start_flag = True
            elif ctrl == "stop":
                self.start_flag = False
            else:
                logging.info(f"Unknown ctrl text '{ctrl}'")

    async def start(self):
        await self.create_queue()
        logging.info("[x] start live detection message queue")
        try:
            # self._consume_tag = await self.queue.consume(callback=self.on_message ,no_ack=True)
            self._control_consume_tag = await self.control_queue.consume(callback=self.on_control_message ,no_ack=False)
            self._publish_task = await asyncio.create_task(self.publish())
        except Exception as e:
            print(e)

    async def cancel(self):
        await self.queue.cancel(self._consume_tag)
        await self._control_consume_tag.cancel(self._control_consume_tag)


class DetectionMessageQueue:
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    queue : AbstractQueue

    def __init__(self, detector, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str):
        super().__init__()
        self.channel = channel
        self.exchange = exchange
        self.callback_exchange = channel.default_exchange
        self.routing_key = routing_key
        self.detector = detector
        self.queue = None

    async def create_queue(self):
        logging.info(" [x] Create temporary queue")

        self.queue = await self.channel.declare_queue(exclusive=True)
        await self.queue.bind(self.exchange, routing_key=self.routing_key)


    async def on_message(self, message: AbstractIncomingMessage):
        logging.info("Detection message queue on request")
        async with message.process():
            body, headers = message.body, message.headers
            img, img_header = decode_img(body, headers)

            detector_output = self.detector.predict(img)

            result = {
                "bboxes" : detector_output["bboxes"],
                "classification" : detector_output["classification"]
            }
                
            body = json.dumps( result )
            headers = {}

            await self.callback_exchange.publish(
                Message(
                    body = body,
                    correlation_id = message.correlation_id,
                    headers = headers,
                ),
                routing_key=message.reply_to
            )
        logging.info("Send AI detection ")

            # the example didn't ack back if process() method is used
            # await message.ask()


    async def start(self):
        await self.create_queue()
        logging.info("[x] start detection message queue")

        self._consume_tag = await self.queue.consume(self.on_message, no_ack=False)
        # async with self.queue.iterator() as qiterator:
        #     message : AbstractIncomingMessage
        #     async for message in qiterator:
        #         try:
        #             await self.on_message(message=message)
        #         except Exception:
        #             logging.exception("Processing error for message %r", message)
