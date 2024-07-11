import asyncio
from aio_pika import Message, connect, ExchangeType
from aio_pika.abc import (
    AbstractChannel, AbstractConnection, AbstractExchange, AbstractQueue, AbstractIncomingMessage, AbstractQueue,
)
import logging
import uuid

from collections import namedtuple
from typing import Callable, List, Dict, Union, Optional

MessageQueueResponse = namedtuple("MessageQueueResponse", ["content", "header"], defaults=[None, None])


class BasicClient:
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    queue : AbstractQueue
    client_name : str
    time_out : float

    def __init__(self, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, client_name:str, time_out:float) -> None:
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.client_name = client_name
        self.time_out = time_out
        self.queue = None

    async def create_queue(self):
        self.callback_queue = await self.channel.declare_queue(exclusive=True)
        self.futures = {}
        logging.info(f"Create {self.client_name} Callback queue: {self.callback_queue.name}")


    async def start(self):
        await self.create_queue()
        # the consume here is not blocking
        await self.callback_queue.consume(self.on_response, no_ack=True)
        logging.info(f"{self.client_name} start up")
        return self

    async def on_response(self, message: AbstractIncomingMessage) -> None:
        if message.correlation_id is None:
            logging.info(f"{self.client_name} receive bad message {message!r}")
            return
        
        logging.info(f"{self.client_name} receive an item ({message.correlation_id})")
        future: asyncio.Future = self.futures.pop(message.correlation_id)
        future.set_result( (message.body, message.headers) )

    @property
    def empty_response(self):
        return MessageQueueResponse(None, None)

    async def get(self, message, headers) -> int:
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        logging.info(f"{self.client_name} request an item ({correlation_id})")

        self.futures[correlation_id] = future

        await self.exchange.publish(
            Message(
                message,
                content_type="text/plain",
                correlation_id=correlation_id,
                reply_to=self.callback_queue.name,
                headers=headers
            ),
            routing_key=self.routing_key,
        )

        # this code block works for python 3.12
        # try:
        #     async with asyncio.timeout():
        #         return await future
        # except TimeoutError:
        #     logging.info("fail to acquire response from the pascal node")

        # https://docs.python.org/3/library/asyncio-task.html#timeouts
        # wait_for is on both 3.10 and 3.12
        # timeout unit is in second
        try:
            return await asyncio.wait_for(future, timeout=self.time_out)
        except asyncio.TimeoutError:
            logging.info("fail to acquire response from the pascal node")
            return self.empty_response
    

class BasicStreamClient:
    channel : AbstractChannel
    exchange : AbstractExchange
    routing_key : str
    control_routing_key : str
    queue : AbstractQueue
    on_response_callback : Callable
    client_name : str
    time_out : float


    def __init__(self, channel:AbstractChannel, exchange:AbstractExchange, routing_key:str, control_routing_key:str, on_response_callback:Callable, client_name:str, time_out:float) -> None:
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.control_routing_key = control_routing_key
        self.queue = None
        self.channel = channel
        self.exchange = exchange
        self.routing_key = routing_key
        self.queue = None
        self.on_response_callback = on_response_callback
        self.client_name = client_name
        self.time_out = time_out

        self._consumer_tag = None

    async def create_queue(self):
        self.queue = await self.channel.declare_queue(exclusive=True)
        await self.queue.bind(self.exchange, routing_key=self.routing_key)
        logging.info(f"{self.client_name} create Queue: {self.queue.name}")

    async def start(self, start_consume_loop=True):
        logging.info("{self.client_name} start live streaming")
        await self.create_queue()

        if start_consume_loop:
            logging.info("{self.client_name} start live streaming consume loop")

            try:
                self._consumer_tag = await self.queue.consume(self.on_response, no_ack=True)
            except Exception as e:
                print(e)

    async def stop(self):
        logging.info(f"{self.client_name} terminate consume")
        if self.queue is not None and self._consumer_tag is not None:
            await self.queue.cancel(self._consumer_tag, )
            await self.queue.delete()

            self._consumer_tag = None
            self.queue = None
        
    async def start_streaming(self):
        await self.exchange.publish(
            message= Message(
                body = "start".encode(),
                headers = {}
            ),
            routing_key= self.control_routing_key
        )

    async def stop_streaming(self):
        await self.exchange.publish(
            message= Message(
                body = "stop".encode(),
                headers={}
            ),
            routing_key= self.control_routing_key
        )

    async def config_streaming(self, configuration):
        await self.exchange.publish(
            message= Message(
                body = "config".encode(),
                headers = configuration
            ),
            routing_key= self.control_routing_key
        )

    async def get(self):
        return await self.queue.get(no_ack=True)
    
    async def on_response(self, message: AbstractIncomingMessage) -> None:
        logging.debug(f"{self.client_name} on live response")

        #TODO : add conditon for filtering bad message
        condition = False
        if condition:
            logging.info(f"{self.client_name} received a bad message {message!r}")
            return

        succ_flag = await self.on_response_callback( message )
        if not succ_flag:
            self.stop()
