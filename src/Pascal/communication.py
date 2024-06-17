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

# from video_stream import VideoCompressor, VideoRecorder
import json
import asyncio

class ChamberLogMessageQueue:
    channel : AbstractChannel
    exchange : AbstractExchange
    callback_exchange : AbstractExchange
    routing_key : str
    queue : AbstractQueue

    def __init__(self, log_reader, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str):
        # super().__init__()
        self.channel = channel
        self.exchange = exchange
        self.callback_exchange = channel.default_exchange
        self.routing_key = routing_key
        self.log_reader = log_reader
        self.queue = None

    async def create_queue(self):
        logging.info(" [x] Create temporary queue")

        self.queue = await self.channel.declare_queue(exclusive=True)
        await self.queue.bind(self.exchange, routing_key=self.routing_key)

    async def on_message(self, message: AbstractIncomingMessage):
        logging.info("[x] On Log Request")
        async with message.process():
            log = self.log_reader.get_log() # this get the latest frame from the peek queue
            body = json.dumps(log).encode()

            await self.callback_exchange.publish(
                Message(
                    body = body,
                    correlation_id=message.correlation_id,
                    headers={},
                ),
                routing_key=message.reply_to
            )
        logging.info("[x] Send Log ")



    async def start(self):
        await self.create_queue()

        logging.info(" [x] Awaiting Chamber Log requests")
        self._consume_tag = await self.queue.consume(self.on_message, no_ack=False)

    async def cancel(self):
        await self.queue.cancel(self._consume_tag)       


class LiveChamberLogMessageQueue:
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    control_routing_key : str
    publish_routing_key : str

    queue : AbstractQueue
    control_queue : AbstractQueue

    def __init__(self, log_reader, log_queue, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, control_routing_key:str, publish_routing_key:str):
        """
            has no input, routing_key could be None
        """
        super().__init__()
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.publish_routing_key = publish_routing_key
        self.control_routing_key = control_routing_key
        self.log_reader = log_reader
        self.log_queue = log_queue

        self.start_flag = False

        self.queue = None
        self.control_queue = None

    async def create_queue(self):
        logging.info(" [x] Create temporary queue")

        # self.queue = await self.channel.declare_queue(exclusive=True)
        # await self.queue.bind(self.exchange, routing_key=self.routing_key)

        self.control_queue = await self.channel.declare_queue(exclusive=True)
        await self.control_queue.bind(self.exchange, routing_key=self.control_routing_key)


    async def on_control_message(self, message: AbstractIncomingMessage):
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

            logging.info("Off Control Message")

            # the example didn't ack back if process() method is used
            # await message.ask()

    async def publish(self,):
        while True:
            log = self.log_queue.get()
            body= json.dumps(log).encode()
            
            if self.start_flag:
                await self.exchange.publish(
                    Message(
                        body = body,
                        headers={},
                    ),
                    routing_key=self.publish_routing_key
                )
                logging.info("Send Live Log ")

            else:
                logging.info("Start Flag is off")
                await asyncio.sleep(0.1)



    async def start(self):
        await self.create_queue()
        logging.info(" [x] Start Live Image Message Queue")
        self._control_consumer_tag = await self.control_queue.consume(self.on_control_message, no_ack=False)
        self._publish_task = asyncio.create_task( self.publish() )


    async def cancel(self):
        await self.control_queue.cancel(self._control_consumer_tag)
        await self._publish_task.cancel()
